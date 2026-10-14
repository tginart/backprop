"""Prompt for a Foundry token without echoing it or leaving input workers alive.

Architecture: select terminal or genuine kernel input -> offer token/offline
choices -> read with a deadline -> restore input state on success, interrupt or
failure. Main caller: login_session when reusable credentials are unavailable or
relogin is requested. Foundry owns account creation; this menu only selects a
logging mode and an existing token. No browser or network request is started.
"""
from __future__ import annotations

import math
import os
import select
import sys
import time
from urllib.parse import urlencode

from .errors import UsageError


def _notebook_kernel():
    """Use the running kernel's input bridge, never an IPython shell stand-in.

    Main callers: prompt_token and _readline. Why this exists: Jupyter streams
    deliberately report non-TTY, but its stdin channel supports password input.
    A terminal IPython shell or configured shell without a kernel cannot provide
    that channel and must still satisfy ordinary terminal requirements.
    """
    module = sys.modules.get('IPython')
    if module is None:
        return None
    shell = getattr(module, 'get_ipython', lambda: None)()
    kernel = getattr(shell, 'kernel', None)
    if not (callable(getattr(kernel, 'raw_input', None))
            and callable(getattr(kernel, 'getpass', None))):
        return None
    return kernel


def _notebook_readline(kernel, message, *, hidden, deadline):
    """Use the existing kernel stdin channel with a bounded polling deadline.

    Main caller: _readline. The kernel sends a password-marked stdin request for
    secrets. Untimed reads delegate to its public input bridge; timed reads use
    the same signed messages and flush stale replies before a new request.
    Why this exists: the pinned timed notebook menu leaves an input worker alive.
    Polling owns no reader thread or process signal and remains interruptible on
    each platform supported by the kernel's existing ZeroMQ socket.
    """
    read = kernel.getpass if hidden else kernel.raw_input
    if deadline is None:
        return read(message)
    if deadline <= time.monotonic():
        raise TimeoutError('Foundry login prompt timed out')
    if not kernel._allow_stdin:
        from IPython.core.error import StdinNotImplementedError
        name = 'getpass' if hidden else 'raw_input'
        raise StdinNotImplementedError(name + ' was called, but this frontend does not support input requests.')
    import zmq
    socket, session = kernel.stdin_socket, kernel.session
    if session is None:
        raise UsageError('Notebook login requires an active kernel stdin session')
    sys.stdout.flush()
    sys.stderr.flush()
    while socket.poll(0, zmq.POLLIN):
        socket.recv_multipart(zmq.NOBLOCK)
    request = session.send(socket, 'input_request', {'prompt': message, 'password': hidden},
        parent=kernel.get_parent('shell'), ident=kernel._parent_ident['shell'])
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Foundry login prompt timed out')
        if not socket.poll(min(10, math.ceil(remaining * 1000)), zmq.POLLIN):
            continue
        _, reply = session.recv(socket, mode=zmq.NOBLOCK)
        if reply is None:
            continue
        parent = reply.get('parent_header', {}).get('msg_id')
        if parent and parent != request['header']['msg_id']:
            continue
        if (reply.get('header', {}).get('msg_type') != 'input_reply' or
                not isinstance(reply.get('content', {}).get('value'), str)):
            raise UsageError('Notebook login received an invalid stdin reply')
        value = reply['content']['value']
        if value == '\x04':
            raise EOFError()
        return value


def _readline(message, *, hidden, deadline):
    """Read one terminal line with no background reader surviving a timeout.

    Main caller: prompt_token. POSIX keeps canonical line editing but disables
    echo for secrets; Windows reads console keys without displaying them.
    """
    kernel = _notebook_kernel()
    if kernel is not None:
        try:
            return _notebook_readline(kernel, message, hidden=hidden, deadline=deadline)
        except (EOFError, KeyboardInterrupt):
            # Click converts kernel EOF cancellation to an interrupt in the
            # pinned public prompt, including login as well as init. Its
            # interrupt message also omits the kernel's internal traceback.
            raise KeyboardInterrupt from None
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise UsageError('No Foundry token configured. Use login(key=...) or an interactive terminal')
    if os.name == 'nt':
        import msvcrt
        sys.stderr.write(message); sys.stderr.flush()
        value = []
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('Foundry login prompt timed out')
            if not msvcrt.kbhit():
                time.sleep(0.01); continue
            char = msvcrt.getwch()
            if char == '\x03': raise KeyboardInterrupt()
            if char == '\x1a':
                if deadline is not None and not hidden:
                    raise TimeoutError('Foundry login prompt timed out')
                raise EOFError()
            if char in {'\r', '\n'}:
                sys.stderr.write('\n'); return ''.join(value)
            if char in {'\x00', '\xe0'}:
                msvcrt.getwch(); continue
            if char == '\b':
                if value: value.pop()
            else:
                value.append(char)
            # Deliberately omit console echo even for the nonsensitive menu.
    else:
        import termios
        descriptor = sys.stdin.fileno()
        original = termios.tcgetattr(descriptor)
        changed = list(original)
        if hidden: changed[3] &= ~(termios.ECHO | termios.ECHONL)
        completed = False
        try:
            termios.tcsetattr(descriptor, termios.TCSANOW, changed)
            sys.stderr.write(message); sys.stderr.flush()
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            if not select.select([descriptor], [], [], remaining)[0]:
                raise TimeoutError('Foundry login prompt timed out')
            # Read directly rather than through buffered stdin: a buffered menu
            # read can consume the following token before select sees it.
            value = bytearray()
            while True:
                char = os.read(descriptor, 1)
                if not char:
                    # Pinned timed menu input treats EOF as timeout, selecting
                    # disabled mode. Secret/untimed cancellation remains an
                    # explicit error rather than silently choosing a mode.
                    if deadline is not None and not hidden:
                        raise TimeoutError('Foundry login prompt timed out')
                    raise EOFError()
                if char == b'\n': break
                value.extend(char)
            decoded = value.decode(sys.stdin.encoding or 'utf-8').rstrip('\r')
            completed = True
            return decoded
        finally:
            if not completed: termios.tcflush(descriptor, termios.TCIFLUSH)
            termios.tcsetattr(descriptor, termios.TCSANOW, original)
            if hidden: sys.stderr.write('\n'); sys.stderr.flush()


def prompt_token(host, *, force=False, timeout=None, referrer=None):
    """Return a pasted token or None for an explicitly selected offline mode.

    Main caller: authenticate_candidate. force removes the offline choice; the
    referrer is retained in the displayed Foundry origin URL, never in events.
    """
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                                or not math.isfinite(timeout) or timeout < 0):
        raise UsageError('login timeout must be a finite nonnegative number')
    deadline = None if timeout is None else time.monotonic() + timeout
    if _notebook_kernel() is None and (not sys.stdin.isatty() or not sys.stderr.isatty()):
        raise UsageError('No Foundry token configured. Use login(key=...) or an interactive terminal')
    if not host:
        raise UsageError('Configure a Foundry host before interactive login')
    url = host + ('/?' + urlencode({'ref': referrer}) if referrer else '')
    sys.stderr.write('Foundry: ' + url + '\n')
    if not force:
        while True:
            choice = _readline('1) Use a Foundry token  2) Continue offline: ', hidden=False, deadline=deadline)
            if choice == '2': return None
            if choice == '1': break
            sys.stderr.write('Choose 1 or 2.\n')
    return _readline('Foundry token (hidden): ', hidden=True, deadline=deadline)
