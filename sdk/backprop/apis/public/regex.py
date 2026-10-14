"""RE2 queries with an explicit dependency boundary for extended syntax.

Architecture: installed google-re2 handles the full documented dialect; a small
portable subset uses Python's regex engine with ASCII shorthand classes and
strict end anchors. Unrecognized syntax requires RE2 instead of guessing.
Main callers: Public Api $regex filters. No packages are installed at runtime.
"""
from functools import lru_cache
import re


@lru_cache(maxsize=128)
def compile_pattern(pattern):
    """Compile a documented query, preserving RE2's validation and matching.

    Why this exists: Python regex alone silently differs for Unicode shorthand
    classes and final newlines, and accepts features RE2 rejects. Full patterns
    use google-re2; the dependency-free route accepts a deliberately small grammar.
    """
    try:
        import re2
    except ImportError:
        re2 = None
    if re2 is not None:
        options = re2.Options()
        options.log_errors = False
        try: return re2.compile(pattern, options=options)
        except re2.error as error: raise ValueError(f"Invalid RE2 $regex: {error}") from error
    if re.search(r"\(\?[=!<]|\\[1-9]|\(\?P=", pattern):
        raise ValueError("$regex does not support lookaround or backreferences")
    # Optional flags, named groups, POSIX/Unicode properties, bounded repeats
    # and unusual escapes belong to the real RE2 engine, not a partial parser.
    if ("(?" in pattern or "{" in pattern or "[[:" in pattern or
            re.search(r"\\[A-Za-z]", pattern) or re.search(r"[+*?][+?]", pattern)):
        raise ImportError("This $regex pattern requires google-re2; use the backprop-foundry[query] optional dependency")
    output, escaped, in_class = [], False, False
    for char in pattern:
        if escaped:
            output.extend(["\\", char]); escaped = False
        elif char == "\\": escaped = True
        elif char == "[": in_class = True; output.append(char)
        elif char == "]": in_class = False; output.append(char)
        elif char == "$" and not in_class: output.append(r"\Z")
        else: output.append(char)
    if escaped: output.append("\\")
    try: return re.compile("".join(output), re.ASCII)
    except re.error as error: raise ValueError(f"Invalid $regex: {error}") from error
