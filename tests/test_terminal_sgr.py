"""Verify styled terminal output survives edits, persistence and public readback.

Architecture: raw split terminal writes -> real emulator/runtime -> immutable
console events and output.log -> PublicApi console lines. Subprocesses isolate
native stdout redirection. No service credentials or external packages are used.
Main callers: Python unittest discovery and the console compatibility audit.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop.apis.public import Api
from backprop.terminal import Terminal


def replay(chunks, max_lines=200):
    terminal, text, edits = Terminal(max_lines=max_lines), "", []
    for chunk in chunks:
        remove, replacement = terminal.feed(chunk)
        text = (text[:-remove] if remove else text) + replacement
        edits.append((remove, replacement))
    return text, edits


class TerminalStyleTests(unittest.TestCase):
    def test_split_sgr_preserves_foreground_background_effects_and_individual_resets(self):
        text, _ = replay(["\x1b[3", "1;44;1;3;4;5;7;9mX", "\x1b[39;49;22;23;24;25;27;29mY\n"])
        self.assertEqual(text, "\x1b[31;44;1;3;4;5;7;9mX\x1b[0mY\n")
        self.assertEqual(replay(["\x1b[1mA\x1b[2mB"])[0], "\x1b[1mA\x1b[0mB")

    def test_style_only_unicode_overwrite_produces_tail_edit_inside_sgr(self):
        text, edits = replay(["\x1b[31m🙂", "\r\x1b[32m🙂"])
        self.assertEqual(text, "\x1b[32m🙂\x1b[0m")
        self.assertEqual(edits[1], (7, "2m🙂\x1b[0m"))

    def test_cursor_erase_keeps_cell_styles_without_recoloring_neighbors(self):
        text, _ = replay(["\x1b[31mred\n\x1b[34mblue", "\x1b[A\r\x1b[32mX\x1b[K"])
        self.assertEqual(text, "\x1b[32mX\x1b[0m\n\x1b[34mblue\x1b[0m")
        text, _ = replay(["\x1b[31mold\ncurrent\n", "\x1b[A\r\x1b[32mnew\x1b[K"], max_lines=2)
        self.assertEqual(text, "\x1b[31mold\x1b[0m\n\x1b[32mnew\x1b[0m\n")

    def test_bright_indexed_rgb_and_osc_controls_are_safe_and_streamable(self):
        text, _ = replay(["\x1b]8;;https://ignored.invalid/", "\x1b\\\x1b[91;104mA\x1b[0m",
                          "\x1b[38;5;120mB\x1b[48;2;1;2;3mC\x1b[0m", "\x1b]0;ignored title\x07D"])
        self.assertEqual(text, "\x1b[91;104mA\x1b[0m\x1b[38;5;120mB\x1b[0m\x1b[38;5;120;48;2;1;2;3mC\x1b[0mD")
        self.assertEqual(replay(["\x1b[38;2;999;0;0mplain"])[0], "plain")

    def test_style_is_carried_to_new_lines_but_serialized_lines_reset(self):
        self.assertEqual(replay(["\x1b[33mfirst\nsecond\n", "\x1b[mplain"])[0],
                         "\x1b[33mfirst\x1b[0m\n\x1b[33msecond\x1b[0m\nplain")

    def test_runtime_and_public_readback_preserve_styled_unicode_edits_per_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = backprop.MemorySink()
            run = backprop.Run(project="style", entity="team", id="edit", dir=directory,
                               settings={"x_disable_stats":True,"console": "wrap_emu", "quiet": True}, _sink=sink, _deployment={})
            try:
                run.log_console("\x1b[31m🙂", "stdout")
                run.log_console("\x1b[33;1mwarning\x1b[0m\n", "stderr")
                run.log_console("\r\x1b[32m🙂\x1b[0m\n", "stdout")
                run.finish()
            finally:
                if not run._closed: run.finish()
            payloads = [event["payload"] for event in sink.events if event["type"] == "console"]
            self.assertEqual(payloads[-1]["replace_tail"], 7)
            reader = Api._from_store(sink, entity="team", project="style").run("edit")
            lines = list(reader.console_logs())
            self.assertEqual([line.content for line in lines], ["\x1b[32m🙂\x1b[0m", "\x1b[33;1mwarning\x1b[0m"])
            self.assertEqual(lines[1].level, "error")
            reference = next(event["payload"]["file"] for event in reversed(sink.events)
                             if event["type"] == "file" and event["payload"]["name"] == "output.log")
            output = sink.download_file(reference).decode()
            # The saved file preserves actual cross-stream chunk order. The API
            # joins unfinished same-stream lines for a readable console view.
            self.assertEqual(output, "\x1b[33;1mwarning\x1b[0m\n\x1b[32m🙂\x1b[0m\n")

    def test_file_renderer_checkpoints_skip_unchanged_scrollback(self):
        from backprop.console_styles import SGRFileRenderer
        with tempfile.TemporaryDirectory() as directory:
            run = backprop.Run(project="style", dir=directory, settings={"x_disable_stats":True,"console": "wrap_emu", "quiet": True},
                               _sink=backprop.MemorySink(), _deployment={})
            try:
                for index in range(100): run.log_console(f"\x1b[31mline {index}\n")
                calls, original = [], SGRFileRenderer.feed
                def tracked(renderer, text, stream):
                    calls.append(text)
                    return original(renderer, text, stream)
                with patch.object(SGRFileRenderer, "feed", tracked):
                    run.log_console("\x1b[32mnew")
                    self.assertEqual(len(calls), 1)
                    calls.clear()
                    run.log_console("\r\x1b[34mnew")
                    self.assertEqual(len(calls), 2)
                    self.assertTrue(all("line 0" not in text for text in calls))
                run.finish()
            finally:
                if not run._closed: run.finish()

    def test_raw_capture_and_native_redirect_keep_original_ansi_bytes(self):
        for mode in ("wrap_raw", "redirect"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                code = """import os, backprop as w
s=w.MemorySink()
r=w.Run(project='style', id='raw', dir=os.environ['TEST_DIR'], settings={'x_disable_stats':True,'console':os.environ['CAPTURE_MODE'],'quiet':True}, _sink=s, _deployment={})
raw='\\x1b[31;1mred🙂\\x1b[0m\\n'
if os.environ['CAPTURE_MODE']=='redirect': os.write(1, raw.encode())
else:
 import sys
 sys.stdout.write(raw)
r.finish()
assert ''.join(e['payload']['text'] for e in s.events if e['type']=='console')==raw
assert not any('replace_tail' in e['payload'] for e in s.events if e['type']=='console')
ref=next(e['payload']['file'] for e in reversed(s.events) if e['type']=='file' and e['payload']['name']=='output.log')
assert s.download_file(ref)==raw.encode()
"""
                result = subprocess.run([sys.executable, "-c", code], env={**os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "sdk"), "TEST_DIR": directory,
                    "CAPTURE_MODE": mode}, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "\x1b[31;1mred🙂\x1b[0m\n")


if __name__ == "__main__": unittest.main()
