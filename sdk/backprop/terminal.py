"""Small streaming terminal renderer for ordinary training progress output.

Architecture: raw chunks -> buffered escape parser -> editable screen -> minimal
tail replacement. Each cell owns its immutable SGR style; canonical escape bytes
preserve that style in events and downloaded logs. Completed scrollback is
committed, while the last 200 lines remain editable for progress output. Tail
lengths count Unicode characters in serialized text, including SGR bytes.
OSC title/hyperlink sequences are discarded and never become executable data.
Main callers: ConsoleCapture when Settings.console is wrap_emu.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class _Style:
    foreground: tuple = ()
    background: tuple = ()
    effects: frozenset = frozenset()

    def escape(self):
        values = (*self.foreground, *self.background, *sorted(self.effects))
        return "\x1b[" + ";".join(map(str, values)) + "m" if values else ""


_DEFAULT = _Style()
_BLANK = (" ", _DEFAULT)


def _render_line(line):
    """Emit style transitions and reset each line to make readback self-contained.

    Main callers: Terminal.feed. A style-only overwrite must change the serialized
    snapshot even when the visible characters stay identical. Per-line resets
    also keep a colored stdout line from coloring an interleaved stderr line.
    """
    output, previous = [], _DEFAULT
    for character, style in line:
        if style != previous:
            if previous != _DEFAULT:
                output.append("\x1b[0m")
            output.append(style.escape())
            previous = style
        output.append(character)
    if previous != _DEFAULT:
        output.append("\x1b[0m")
    return "".join(output)


def apply_sgr(style, parameters):
    """Apply bounded SGR values, keeping style state across split writes.

    Main callers: Terminal._command and SGRFileRenderer.feed. Codes 2/22 clear bold like the pinned renderer;
    compound parameters and indexed/RGB colors are accepted as standard SGR.
    Malformed extended colors never turn their channel bytes into effects.
    """
    if len(parameters) > 256 or not re.fullmatch(r"[0-9;]*", parameters):
        return style
    values = [int(value) if value else 0 for value in parameters.split(";")]
    foreground, background, effects = style.foreground, style.background, set(style.effects)
    index = 0
    off = {2: 1, 22: 1, 23: 3, 24: 4, 25: 5, 27: 7, 29: 9}
    while index < len(values):
        value = values[index]
        index += 1
        if value == 0:
            foreground, background, effects = (), (), set()
        elif value in {38, 48}:
            if index >= len(values) or values[index] not in {2, 5}:
                break
            count = 3 if values[index] == 2 else 1
            end = index + count + 1
            channels = values[index+1:end]
            if len(channels) != count or any(channel > 255 for channel in channels):
                break
            color = (value, *values[index:end])
            if value == 38: foreground = color
            else: background = color
            index = end
        elif 30 <= value <= 37 or 90 <= value <= 97: foreground = (value,)
        elif 40 <= value <= 47 or 100 <= value <= 107: background = (value,)
        elif value == 39: foreground = ()
        elif value == 49: background = ()
        elif value in {1, 3, 4, 5, 7, 9}: effects.add(value)
        elif value in off: effects.discard(off[value])
    return _Style(foreground, background, frozenset(effects))


class Terminal:
    def __init__(self, max_lines=200):
        self.lines = [[]]
        self.row = self.column = 0
        self.saved = (0, 0)
        self.pending = ""
        self.visible = ""
        self.max_lines = max_lines
        self.style = _DEFAULT

    def _line(self):
        while len(self.lines) <= self.row:
            self.lines.append([])
        return self.lines[self.row]

    def _write(self, character):
        line = self._line()
        if len(line) <= self.column:
            line.extend(_BLANK for _ in range(self.column-len(line)+1))
        line[self.column] = (character, self.style)
        self.column += 1

    def _command(self, parameters, command):
        if parameters.startswith("?"):
            return
        if command == "m":
            self.style = apply_sgr(self.style, parameters)
            return
        try:
            values = [int(value) if value else 0 for value in parameters.split(";")]
        except ValueError:
            return
        amount = (values[0] if values else 0) or 1
        if command == "A": self.row = max(0, self.row-amount)
        elif command == "B": self.row += amount
        elif command == "C": self.column += amount
        elif command == "D": self.column = max(0, self.column-amount)
        elif command == "E": self.row, self.column = self.row+amount, 0
        elif command == "F": self.row, self.column = max(0, self.row-amount), 0
        elif command == "G": self.column = max(0, amount-1)
        elif command in {"H", "f"}:
            self.row = max(0, amount-1)
            self.column = max(0, ((values[1] if len(values)>1 else 1) or 1)-1)
        elif command == "s": self.saved = (self.row, self.column)
        elif command == "u": self.row, self.column = self.saved
        elif command == "L":
            # The pinned W&B renderer inserts blank lines after the cursor's
            # current line while retaining that line's existing characters.
            self._line()
            self.lines[self.row+1:self.row+1] = [[] for _ in range(amount)]
        elif command == "K":
            line = self._line()
            mode = values[0]
            if mode == 0: del line[self.column:]
            elif mode == 1:
                for index in range(min(self.column+1, len(line))): line[index] = _BLANK
            elif mode == 2: self.lines[self.row] = []
        elif command == "J":
            mode = values[0]
            if mode in {2, 3}:
                self.lines = [[] for _ in range(self.row+1)]
            elif mode == 0:
                del self._line()[self.column:]
                del self.lines[self.row+1:]
            elif mode == 1:
                for row in range(min(self.row, len(self.lines))): self.lines[row] = []
                line = self._line()
                for index in range(min(self.column+1, len(line))): line[index] = _BLANK
        # Cursor visibility and unknown commands do not produce text.

    def feed(self, text):
        """Return a Unicode tail edit over the styled serialized console text.

        Main callers: ConsoleCapture._write_locked. Edits may cross an SGR
        sequence, so consumers must apply edits before parsing display styles.
        """
        data, self.pending = self.pending + text, ""
        index = 0
        while index < len(data):
            char = data[index]
            if char == "\x1b":
                if index+1 == len(data):
                    self.pending = data[index:]
                    break
                if data[index+1] == "[":
                    match = re.match(r"\x1b\[([0-?]*)([ -/]*)([@-~])", data[index:])
                    if not match:
                        self.pending = data[index:]
                        break
                    self._command(match[1], match[3])
                    index += len(match[0])
                    continue
                if data[index+1] == "]":
                    end = re.search(r"\x07|\x1b\\", data[index+2:])
                    if not end:
                        self.pending = data[index:]
                        break
                    index += 2+end.end()
                    continue
                if data[index+1] == "7": self.saved = (self.row, self.column)
                elif data[index+1] == "8": self.row, self.column = self.saved
                index += 2
                continue
            if char == "\r": self.column = 0
            elif char == "\n": self.row, self.column = self.row+1, 0; self._line()
            elif char == "\b": self.column = max(0, self.column-1)
            elif char == "\t":
                for _ in range(8-self.column%8): self._write(" ")
            elif ord(char) >= 32: self._write(char)
            index += 1
        rendered = "\n".join(_render_line(line) for line in self.lines)
        prefix = 0
        for left, right in zip(self.visible, rendered):
            if left != right: break
            prefix += 1
        replacement = len(self.visible)-prefix, rendered[prefix:]
        if len(self.lines) > self.max_lines:
            count = len(self.lines)-self.max_lines
            self.lines = self.lines[count:]
            self.row = max(0, self.row-count)
            self.saved = (max(0, self.saved[0]-count), self.saved[1])
        self.visible = "\n".join(_render_line(line) for line in self.lines)
        return replacement
