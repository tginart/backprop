"""Own a real Windows pseudoconsole using only the standard library.

Architecture: Win32 pipes -> CreatePseudoConsole -> attached CreateProcessW
child -> continuously drained VT output -> checked child and handle teardown.
Main callers: test_framework_init_windows_contracts. Why this exists: ordinary
subprocess pipes are not terminals, and a mocked msvcrt cannot establish actual
console EOF, hidden input, signal handling or console-mode restoration.

The output reader belongs to the test driver, never to the SDK child. It stays
alive while ClosePseudoConsole emits its final output, then must join. Input
bytes are deliberately never stored in retained driver observations.
"""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import subprocess
import threading


class COORD(ctypes.Structure):
    _fields_ = [('X', wintypes.SHORT), ('Y', wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('lpReserved', wintypes.LPWSTR),
        ('lpDesktop', wintypes.LPWSTR), ('lpTitle', wintypes.LPWSTR),
        ('dwX', wintypes.DWORD), ('dwY', wintypes.DWORD),
        ('dwXSize', wintypes.DWORD), ('dwYSize', wintypes.DWORD),
        ('dwXCountChars', wintypes.DWORD), ('dwYCountChars', wintypes.DWORD),
        ('dwFillAttribute', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
        ('wShowWindow', wintypes.WORD), ('cbReserved2', wintypes.WORD),
        ('lpReserved2', ctypes.POINTER(wintypes.BYTE)),
        ('hStdInput', wintypes.HANDLE), ('hStdOutput', wintypes.HANDLE),
        ('hStdError', wintypes.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [('StartupInfo', STARTUPINFOW), ('lpAttributeList', ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [('hProcess', wintypes.HANDLE), ('hThread', wintypes.HANDLE),
               ('dwProcessId', wintypes.DWORD), ('dwThreadId', wintypes.DWORD)]


def module_source_evidence(module):
    """Record actual import provenance without inventing built-in source files.

    Main callers: the Windows child probe and portable metadata regression.
    Why this exists: CPython's msvcrt is built-in and has no __file__. Its
    import-spec origin remains evidence; the probe separately hashes the actual
    interpreter executable rather than claiming a nonexistent extension hash.
    """
    spec = module.__spec__
    if spec.origin == 'built-in':
        return {'origin': spec.origin, 'sha256': None}
    if not spec.has_location:
        raise ValueError('No hashable source location for module ' + module.__name__)
    return {'origin': spec.origin,
            'sha256': hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest()}


def kernel_api():
    """Declare pointer-width-safe Windows signatures before allocating handles.

    Main caller: ConsoleProcess. Missing Windows/ConPTY capabilities fail rather
    than selecting a pipe or simulated keyboard fallback.
    """
    if os.name != 'nt':
        raise RuntimeError('Actual Windows ConPTY is required')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreatePipe': ([ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        'CreatePseudoConsole': ([COORD, wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], ctypes.c_long),
        'ClosePseudoConsole': ([wintypes.HANDLE], None),
        'InitializeProcThreadAttributeList': ([ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
        'UpdateProcThreadAttribute': ([ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p], wintypes.BOOL),
        'DeleteProcThreadAttributeList': ([ctypes.c_void_p], None),
        'CreateProcessW': ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)], wintypes.BOOL),
        'ReadFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        'WriteFile': ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        'GetExitCodeProcess': ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        'TerminateProcess': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


class ConsoleProcess:
    """Attach one genuine Python process and release all owned Windows handles.

    Main caller: the Windows contract test. Continuous output draining prevents
    startup/close deadlocks documented by Microsoft. A forced kill is retained
    as a driver failure; successful observations require ordinary child exit.
    """
    def __init__(self, arguments, *, environment, cwd):
        self.api = kernel_api()
        self.handles, self.output_chunks, self.reader_errors = [], [], []
        self.console = wintypes.HANDLE()
        self.process = PROCESS_INFORMATION()
        self.attribute_buffer = None
        self.attributes_initialized = False
        self.reader = None
        self.closed = False
        self.forced_termination = False
        try:
            input_read, self.input_write = self._pipe()
            self.output_read, output_write = self._pipe()
            self.reader = threading.Thread(target=self._read_output, name='framework-windows-conpty-output')
            self.reader.start()
            result = self.api.CreatePseudoConsole(COORD(240, 80), input_read, output_write, 0, ctypes.byref(self.console))
            if result < 0:
                raise OSError('CreatePseudoConsole failed: ' + hex(result & 0xffffffff))
            size = ctypes.c_size_t()
            self.api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            if ctypes.get_last_error() != 122 or not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            self.attribute_buffer = ctypes.create_string_buffer(size.value)
            self._check(self.api.InitializeProcThreadAttributeList(self.attribute_buffer, 1, 0, ctypes.byref(size)))
            self.attributes_initialized = True
            self._check(self.api.UpdateProcThreadAttribute(self.attribute_buffer, 0, 0x00020016,
                self.console, ctypes.sizeof(self.console), None, None))
            startup = STARTUPINFOEXW()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            # Suppress redirected-parent std-handle duplication. Null startup
            # handles let the attached ConPTY create genuine console handles;
            # otherwise child stderr can escape into the CI parent's pipe.
            # Microsoft Terminal's maintainer documents this launch boundary:
            # https://github.com/microsoft/terminal/discussions/15814
            startup.StartupInfo.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
            startup.lpAttributeList = ctypes.addressof(self.attribute_buffer)
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(arguments))
            environment_block = ctypes.create_unicode_buffer('\0'.join(
                key + '=' + value for key, value in sorted(environment.items(), key=lambda item: item[0].upper())) + '\0\0')
            self._check(self.api.CreateProcessW(None, command, None, None, False, 0x00080000 | 0x00000400,
                environment_block, str(cwd), ctypes.byref(startup.StartupInfo), ctypes.byref(self.process)))
            self.handles.extend([self.process.hProcess, self.process.hThread])
            self._close_handle(input_read)
            self._close_handle(output_write)
        except BaseException:
            self.close()
            raise

    def _check(self, result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    def _pipe(self):
        reader, writer = wintypes.HANDLE(), wintypes.HANDLE()
        self._check(self.api.CreatePipe(ctypes.byref(reader), ctypes.byref(writer), None, 0))
        self.handles.extend([reader.value, writer.value])
        return reader.value, writer.value

    def _close_handle(self, handle):
        if handle in self.handles:
            self._check(self.api.CloseHandle(handle))
            self.handles.remove(handle)

    def _read_output(self):
        """Drain final ConPTY frames until its actual output pipe breaks.

        Main caller: the parent-owned reader thread. Unexpected Windows errors
        are retained and checked; only documented broken-pipe EOF is accepted.
        """
        buffer, count = ctypes.create_string_buffer(65536), wintypes.DWORD()
        while True:
            result = self.api.ReadFile(self.output_read, buffer, len(buffer), ctypes.byref(count), None)
            if not result:
                error = ctypes.get_last_error()
                if error != 109:
                    self.reader_errors.append(error)
                return
            if not count.value:
                return
            self.output_chunks.append(buffer.raw[:count.value])

    @property
    def output(self):
        return b''.join(self.output_chunks)

    def send(self, data):
        """Write actual input bytes without retaining credential material."""
        count = wintypes.DWORD()
        self._check(self.api.WriteFile(self.input_write, data, len(data), ctypes.byref(count), None))
        if count.value != len(data):
            raise OSError('ConPTY input write was incomplete')

    def poll(self):
        status = self.api.WaitForSingleObject(self.process.hProcess, 0)
        if status == 258:
            return None
        if status != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = wintypes.DWORD()
        self._check(self.api.GetExitCodeProcess(self.process.hProcess, ctypes.byref(code)))
        return code.value

    def close(self):
        """Stop only a stranded child, drain ConPTY close, then join the reader.

        Main caller: the test's finally block and failed construction. Ordinary
        success requires forced_termination=False and no reader_errors.
        """
        if self.closed:
            return
        self.closed = True
        if self.process.hProcess and self.poll() is None:
            self.forced_termination = True
            self._check(self.api.TerminateProcess(self.process.hProcess, 97))
            if self.api.WaitForSingleObject(self.process.hProcess, 5000) != 0:
                raise TimeoutError('Windows console child did not terminate')
        # Release parent duplicates before closing the pseudoconsole, keeping
        # the reader's handle open for its final frame and broken-pipe EOF.
        for handle in list(self.handles):
            if handle != getattr(self, 'output_read', None):
                self._close_handle(handle)
        if self.console.value:
            self.api.ClosePseudoConsole(self.console)
            self.console = wintypes.HANDLE()
        if self.reader is not None:
            self.reader.join(timeout=5)
            if self.reader.is_alive():
                raise TimeoutError('ConPTY output reader survived close')
        self._close_handle(getattr(self, 'output_read', None))
        if self.attributes_initialized:
            self.api.DeleteProcThreadAttributeList(self.attribute_buffer)
            self.attributes_initialized = False
