"""History result objects independent of Foundry's transport format.

Architecture: Run snapshots provide rows; HistoryScan enforces key/step windows;
optional dataframe/export packages are imported only when explicitly requested.
Main callers: public Run.history/scan_history and Runs.histories.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
import math
from pathlib import Path
import importlib

from .query import Collection


class HistoryScan:
    def __init__(self, rows, *, keys=None, min_step=0, max_step=None, page_size=1000):
        if keys is not None and (not isinstance(keys, list) or not all(isinstance(key, str) for key in keys)):
            raise ValueError("keys must be a list of strings")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size <= 0:
            raise ValueError("page_size must be a positive integer")
        if not isinstance(min_step, int) or min_step < 0 or max_step is not None and not isinstance(max_step, int):
            raise ValueError("History step bounds must be integers; min_step must be nonnegative")
        self.page_size = page_size
        self._max_step = max_step
        self._rows = [({key: row[key] for key in keys} if keys else dict(row)) for row in rows
                      if row.get("_step", 0) >= min_step and (max_step is None or row.get("_step", 0) < max_step)
                      and (not keys or all(key in row for key in keys))]
        self._position = 0

    @property
    def max_step(self): return self._max_step - 1 if self._max_step is not None else None
    def __iter__(self): self._position = 0; return self
    def __next__(self):
        if self._position >= len(self._rows): raise StopIteration
        value = self._rows[self._position]
        self._position += 1
        return dict(value)


@dataclass(frozen=True)
class ConsoleLogLine:
    number: int
    timestamp: datetime | None
    level: str
    label: str
    content: str


class ConsoleLogs(Collection):
    pass


def console_lines(events):
    """Replay terminal tail edits before joining fragmented stdout/stderr lines.

    Main callers: public Run.console_logs. Terminal emulation can revise prior
    progress output. Each edit removes Unicode characters from that stream only;
    segments preserve the order and timestamps of surviving cross-stream writes.
    """
    segments = []
    for event in events:
        stream = event.get("stream", "stdout")
        remaining = event.get("replace_tail", 0)
        if not isinstance(remaining, int) or remaining < 0:
            raise ValueError("Invalid console tail edit")
        for segment in reversed(segments):
            if not remaining: break
            if segment["stream"] != stream: continue
            count = min(remaining, len(segment["text"]))
            if count: segment["text"] = segment["text"][:-count]
            remaining -= count
        if remaining: raise ValueError("Console tail edit exceeds its stream content")
        if event.get("text"):
            segments.append({"stream": stream, "text": event["text"], "timestamp": event.get("timestamp")})
    lines, pending = [], {}
    for segment in segments:
        stream = segment["stream"]
        try: recorded = datetime.fromisoformat(segment["timestamp"].replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError): recorded = None
        for fragment in segment["text"].splitlines(keepends=True):
            if stream not in pending:
                pending[stream] = len(lines)
                lines.append({"number": len(lines), "timestamp": recorded,
                              "level": "error" if stream == "stderr" else "", "label": "", "content": ""})
            lines[pending[stream]]["content"] += fragment.rstrip("\r\n")
            if fragment.endswith(("\n", "\r")): pending.pop(stream)
    return [ConsoleLogLine(**line) for line in lines]


class IncompleteRunHistoryError(Exception):
    """A live run cannot promise a final, complete exported history."""


@dataclass(frozen=True)
class DownloadHistoryResult:
    paths: list[Path]
    contains_live_data: bool
    errors: dict[Path, str] | None = None


def combined_histories(runs, samples=500, keys=None, x_axis="_step", format="default", stream="default"):
    """Build each run's dataframe before combining its sampled history.

    Main callers: public Runs.histories. Why this exists: each provider infers
    its own schema per run, then applies its concatenation rules. Flattening rows
    first changes pandas dtypes and Polars schema failures. Import the requested
    provider before reading runs; an unavailable package cannot consume reads.
    """
    if format not in ("default", "pandas", "polars"):
        raise ValueError(f"Invalid format: {format}. Must be one of 'default', 'pandas', 'polars'")
    provider = None
    if format != "default":
        import importlib
        try:
            provider = importlib.import_module(format)
        except ModuleNotFoundError as error:
            if error.name != format:
                raise
            from ...errors import Error
            raise Error(f"Exporting {format} DataFrame requires {format}") from None
    histories = []
    for run in runs:
        rows = run.history(samples=samples, keys=keys, x_axis=x_axis, pandas=False, stream=stream)
        if not rows:
            continue
        if format == "default":
            for row in rows:
                row["run_id"] = run.id
            histories.extend(rows)
        elif format == "pandas":
            frame = provider.DataFrame.from_records(rows)
            frame["run_id"] = run.id
            histories.append(frame)
        else:
            frame = provider.from_records(rows)
            histories.append(frame.with_columns(provider.lit(run.id).alias("run_id")))
    if format == "default":
        return histories
    if not histories:
        return provider.DataFrame()
    if format == "pandas":
        combined = provider.concat(histories)
        combined.reset_index(drop=True, inplace=True)
        return combined[sorted(combined.columns)]
    combined = provider.concat(histories, how="vertical")
    return combined.select(sorted(combined.columns))


def dataframe(rows, format, *, optional=False):
    """Construct a dataframe, separating missing packages from provider failures.

    Main caller: public Run.history. Why this exists: an ImportError raised
    inside dataframe construction is a real failure, not evidence that pandas
    is absent. Only an absent requested package returns rows with a terminal
    warning. Broken imports and conversion failures reach the public error
    decorator, just as they do after the pinned lazy pandas import resolves.
    """
    if format == "default": return rows
    if format not in {"pandas", "polars"}: raise ValueError("format must be default, pandas or polars")
    try:
        provider = importlib.import_module(format)
    except ModuleNotFoundError as error:
        if error.name != format:
            raise
        if optional:
            import backprop
            backprop.termwarn("Unable to load pandas, call history with pandas=False")
            return rows
        raise ImportError(f"{format} is required for this requested dataframe format") from None
    if format == "pandas":
        return provider.DataFrame.from_records(rows)
    return provider.from_dicts(rows) if rows else provider.DataFrame()


def parquet_table(rows, arrow):
    """Keep native Arrow columns, encoding mixed-type columns as tagged JSON.

    Main callers: Run.download_history_exports. A metric may change from a
    number to text or rich media across steps; Arrow requires one column type.
    The schema identifies JSON columns so every original cell remains available
    without coercing numeric values to text indistinguishable from logged text.
    Foundry's export representation is an intentional storage substitution.
    """
    def json_value(value):
        if type(value) in {tuple, set, frozenset}:
            from ...sequence_codec import encode_sequence
            return encode_sequence(value, json_value)
        if isinstance(value, float) and not math.isfinite(value):
            return {"_type": "number", "value": "NaN" if math.isnan(value) else "Infinity" if value > 0 else "-Infinity"}
        if isinstance(value, (datetime, date)):
            return {"_type": "timestamp", "value": value.isoformat()}
        if type(value).__module__.startswith("numpy") and hasattr(value, "shape"):
            return {"_type": "ndarray", "data": json_value(value.tolist()),
                    "dtype": str(value.dtype), "shape": list(value.shape)}
        if isinstance(value, dict): return {key: json_value(item) for key, item in value.items()}
        if isinstance(value, list): return [json_value(item) for item in value]
        return value

    def needs_type_tags(value):
        """Keep values whose Python identity Arrow would silently flatten.

        Main callers: the per-column export below. Tables can contain declared
        tuple/set constants and shaped arrays, so successful Arrow inference
        alone does not prove that exporting the column preserves its cells.
        """
        if type(value) in {tuple, set, frozenset}: return True
        if type(value).__module__.startswith("numpy") and hasattr(value, "shape"): return True
        if isinstance(value, dict): return any(needs_type_tags(item) for item in value.values())
        if isinstance(value, list): return any(needs_type_tags(item) for item in value)
        return False

    def has_empty_struct(dtype):
        if arrow.types.is_struct(dtype):
            return dtype.num_fields == 0 or any(has_empty_struct(field.type) for field in dtype)
        if (arrow.types.is_list(dtype) or arrow.types.is_large_list(dtype)
                or arrow.types.is_fixed_size_list(dtype)):
            return has_empty_struct(dtype.value_type)
        if arrow.types.is_map(dtype):
            return has_empty_struct(dtype.key_type) or has_empty_struct(dtype.item_type)
        if arrow.types.is_dictionary(dtype): return has_empty_struct(dtype.value_type)
        return False

    columns, encoded = {}, []
    for name in sorted({key for row in rows for key in row}):
        values = [row.get(name) for row in rows]
        use_json = any(needs_type_tags(value) for value in values)
        if not use_json:
            try:
                column = arrow.array(values)
                # Parquet cannot write Arrow structs with no child fields.
                use_json = has_empty_struct(column.type)
            except (arrow.ArrowInvalid, arrow.ArrowTypeError, arrow.ArrowNotImplementedError, OverflowError):
                use_json = True
        if use_json:
            encoded.append(name)
            column = arrow.array([None if value is None else json.dumps(json_value(value), allow_nan=False, separators=(",", ":"))
                                  for value in values], type=arrow.string())
        columns[name] = column
    table = arrow.table(columns)
    metadata = {b"backprop.format": b"history-v1", b"backprop.json_columns": json.dumps(encoded).encode()}
    return table.replace_schema_metadata(metadata)
