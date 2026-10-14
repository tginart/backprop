"""Compact terminal feedback with bounded history and summary presentation.

Architecture: committed rows retain a small numeric tail per metric -> finish
formats a limited number of sparklines and summary leaves -> terminal preferences
control visibility, colors and emoji. No completed-run summary callback or
network read is needed. Main callers: Run replay/commit and finish.
"""
from __future__ import annotations

import math
import sys

from .metrics import leaves
from .scalar_values import decode_numbers, numeric_value


def record_history(run, metrics):
    for path, value in leaves(metrics):
        number = numeric_value(value)
        try:
            finite = number is not None and math.isfinite(number)
        except OverflowError:
            finite = False  # Preserve the integer in history/summary, omit only its sparkline.
        if finite:
            key = ".".join(path)
            history = run._sparklines.setdefault(key, [])
            history.append(number)
            del history[:-40]


def _sparkline(values):
    low, high = min(values), max(values)
    scale = max(abs(low), abs(high), 1)
    span = high/scale-low/scale
    return "".join("▁▂▃▄▅▆▇█"[0 if span == 0 else min(7, int((value/scale-low/scale)/span*7))] for value in values)


def print_footer(run, *, quiet=None):
    """Print one bounded footer after console capture has stopped.

    Main callers: Run.finish's cleanup path, including upload timeout. Quiet
    hides informational rows; silent suppresses every row. Warning summaries
    have their own visibility flag and configured count.
    """
    settings = run._settings
    if run.disabled or settings.silent:
        return
    quiet = settings.quiet if quiet is None else quiet
    color = settings.show_colors if settings.show_colors is not None else sys.stderr.isatty()
    emoji = settings.show_emoji if settings.show_emoji is not None else sys.stderr.isatty()
    prefix = "Backprop: "
    if color:
        prefix = "\x1b[36m" + prefix + "\x1b[0m"
    if settings.show_info and not quiet:
        pending = run._journal is not None and run._journal.ack < run._journal.count
        print(prefix + ("✓ " if emoji and not pending else "") + run.project + "/" + run.id
              + (" (queued locally)" if pending else " (finished)"), file=sys.stderr)
        for key, history in sorted(run._sparklines.items())[:settings.max_end_of_run_history_metrics]:
            print(prefix + "history " + key + " " + _sparkline(history), file=sys.stderr)
        summary = []
        for path, value in leaves(run.summary._data):
            if any(part.startswith("_") for part in path):
                continue
            value = decode_numbers(value)
            if value is None or isinstance(value, (str, bool, int, float)):
                text = f"{value:.6g}" if isinstance(value, float) else str(value)
                summary.append((".".join(path), text[:100]))
        for key, text in sorted(summary)[:settings.max_end_of_run_summary_metrics]:
            print(prefix + "summary " + key + ": " + text, file=sys.stderr)
    if settings.show_warnings and settings.summary_warnings:
        for warning in run._warnings[-settings.summary_warnings:]:
            print(prefix + "WARNING " + warning, file=sys.stderr)
