"""Serialize interleaved emulated streams without splitting ANSI sequences.

Architecture: immutable per-stream style/parser state -> record checkpoint ->
canonical styled fragment. ConsoleCapture restarts at the first edited record,
so only the changed file suffix is rewritten. Raw/redirect capture bypasses this
helper and retains original bytes. Main caller: runtime.ConsoleCapture.
"""
from __future__ import annotations

import re

from .terminal import _DEFAULT, _render_line, apply_sgr


class SGRFileRenderer:
    def __init__(self):
        self.streams = {}

    def copy(self):
        """Checkpoint immutable styles and partial escapes before a file record."""
        result = SGRFileRenderer()
        result.streams = self.streams.copy()
        return result

    def feed(self, text, stream):
        """Return complete SGR fragments without moving any visible characters.

        Main callers: ConsoleCapture._write_file. Tail edits can leave the first
        half of a color escape before stderr and its second half after stderr.
        Buffer only that stream's incomplete escape; output actual characters in
        record order with independent style/reset boundaries. Malformed canonical
        input is an explicit error because it indicates an internal contract bug.
        """
        style, pending = self.streams.get(stream, (_DEFAULT, ""))
        data, pending, index, output, line = pending + text, "", 0, [], []
        while index < len(data):
            character = data[index]
            if character == "\x1b":
                match = re.match(r"\x1b\[([0-9;]*)m", data[index:])
                if match:
                    style = apply_sgr(style, match[1])
                    index += len(match[0])
                    continue
                fragment = data[index:]
                if fragment == "\x1b" or re.fullmatch(r"\x1b\[[0-9;]*", fragment):
                    pending = fragment
                    break
                raise ValueError("Invalid canonical SGR in emulated console file output")
            if character == "\n":
                output.append(_render_line(line) + "\n")
                line = []
            else:
                line.append((character, style))
            index += 1
        output.append(_render_line(line))
        self.streams[stream] = (style, pending)
        return "".join(output)
