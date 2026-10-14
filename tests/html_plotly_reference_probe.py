"""Compare public HTML/Plotly file contracts in separate pinned SDK processes.

Architecture: hash-pinned classes -> real files and binding options -> actual
artifact manifests. A recording run replaces only service publication. Aliases
are called independently. Explicit intentional repairs are reported separately.
Main caller: test_optional_html_plotly. No W&B services or credentials are used.
"""
import hashlib
import importlib
import inspect
import io
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

reference = sys.argv[1] == "reference"
if reference:
    import wandb as w
    assert w.__version__ == "0.30.0"
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()


def outcome(call):
    try:
        return {"value": call()}
    except Exception as error:
        return {"error": type(error).__name__}


def signature(call):
    return [(p.name, p.kind.name, "required" if p.default is p.empty else repr(p.default))
            for p in inspect.signature(call).parameters.values()]


class ProbeRun:
    def __init__(self, directory, link=False, symlink=False):
        self.dir = str(directory)
        self.published = []
        self._settings = SimpleNamespace(allow_media_symlink=link, symlink=symlink)
    def _publish_file(self, path):
        self.published.append(path)


def figure(spec=None):
    # Public Figure serialization is genuine class code; optional plotting
    # runtime behavior has separate tests with actual Plotly/matplotlib.
    return type("Figure", (), {"__module__": "plotly.graph_objs._figure",
        "to_plotly_json": lambda self: spec if spec is not None else {"data": [{"type": "scatter", "x": [1, 2], "y": [3, 4]}]}})()


def make(cls):
    return cls("<h1>frozen</h1>", inject=False) if cls.__name__ == "Html" else cls(figure())


out = {"aliases": {}, "classes": {}, "html_inputs": []}
# Compare actual pinned constructor bytes, including its pydantic-first encoder.
from datetime import date, datetime, timezone
import enum
class Color(enum.Enum): BLUE = "blue"
payloads = [{"layout": {"meta": v}} for v in [Color.BLUE, date(2026, 1, 2), datetime(2026, 1, 2, tzinfo=timezone.utc),
    {2, 3}, b"ab", "café", float("nan"), float("inf"), object()]]
try:
    import numpy as np
except ImportError:
    pass
else:
    payloads += [{"data": [{"x": np.arange(64), "y": np.linspace(1, 4, 64)}]},
                 {"layout": {"meta": [np.int32(7), np.float32(1.5), np.datetime64("2026-01-02")]}}]
out["plotly_bytes"] = [outcome(lambda v=v: Path(w.Plotly(figure(v))._path).read_text()) for v in payloads]
with tempfile.TemporaryDirectory() as directory:
    directory = Path(directory)
    boundary = patch.object(w, "Run", ProbeRun) if reference else patch.object(importlib.import_module("backprop.run"), "Run", ProbeRun)
    with boundary:
        for cls in [w.Html, w.Plotly]:
            name = cls.__name__
            if reference:
                out.setdefault("source_hashes", {})[name] = hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest()
            members = ["__init__", "bind_to_run", "file_is_set", "is_bound", "path_is_reference", "to_json", "from_json", "captions", "get_media_subdir", "with_suffix"]
            if name == "Html": members += ["seq_to_json", "inject_head"]
            row = {"binding": []}
            out["classes"][name] = row
            for module in ["wandb", "wandb.data_types", "wandb.sdk.data_types." + name.lower()]:
                alias = getattr(importlib.import_module(module), name)
                value = make(alias)
                run = ProbeRun(directory / (name + module))
                value.bind_to_run(run, "alias", 2, id_="fixed")
                artifact = w.Artifact("alias-" + name.lower(), "dataset")
                record = {"same_class": alias is cls, "signatures": {m: signature(getattr(alias, m)) for m in members},
                    "bound": value.is_bound(), "file": value.file_is_set(), "json": value.to_json(run_or_artifact=run),
                    "artifact": value.to_json(artifact), "captions": alias.captions([value]),
                    "reference": alias.path_is_reference("s3://bucket/key"), "directory": alias.get_media_subdir(),
                    "suffix": alias.with_suffix("item")}
                owner = SimpleNamespace(get_entry=lambda path: SimpleNamespace(download=lambda: value._path))
                record["decode"] = outcome(lambda: alias.from_json(json_obj=record["artifact"], source_artifact=owner).file_is_set())
                if name == "Html":
                    record["batch"] = alias.seq_to_json([value], run, "key", 3)
                out["aliases"][module + "." + name] = record
            for i, (identity, ignore, link, symlink) in enumerate([(None, None, False, False), (0, False, False, False), ("chosen", True, False, False), ("linked", True, True, False), ("symlink", True, True, True)]):
                value = make(cls)
                source = Path(value._path)
                run, other = ProbeRun(directory / f"{name}-{i}", link, symlink), ProbeRun(directory / f"{name}-{i}-other")
                binding = {"before": [value.file_is_set(), value.is_bound()], "unbound_json": outcome(lambda: value.to_json(run)),
                    "missing_owner": outcome(value.to_json), "invalid_owner": value.to_json(None)}
                binding["return"] = value.bind_to_run(run, "/../nested/key", "step", id_=identity, ignore_copy_err=ignore)
                binding.update(json=value.to_json(run), after=[value.file_is_set(), value.is_bound()],
                    source_exists=source.exists(), published=list(run.published), bytes=Path(value._path).read_text(),
                    wrong_owner=outcome(lambda: value.to_json(other)))
                binding["same_file"] = outcome(lambda: value.bind_to_run(run, "/../nested/key", "step", id_=identity, ignore_copy_err=ignore))
                binding["ignore_same"] = value.bind_to_run(run, "/../nested/key", "step", id_=identity, ignore_copy_err=True)
                artifact = w.Artifact(f"{name.lower()}-{i}", "dataset")
                binding["artifact"] = value.to_json(artifact)
                binding["artifact_repeat"] = value.to_json(artifact)
                binding["artifact_bytes"] = Path(value._path).read_text()
                value.bind_to_run(other, "cross", 8, id_="other")
                binding["cross_json"] = value.to_json(other)
                binding["old_owner"] = outcome(lambda: value.to_json(run))
                row["binding"].append(binding)
            value = make(cls)
            row["paths"] = [outcome(lambda p=p: cls.path_is_reference(p)) for p in [None, Path("https://a"), "s3://a", "gs://a", "http://a", "https://a", "ftp://a", "file:///a", "", {}, 1, b"http://a"]]
            row["captions"] = [outcome(lambda: cls.captions([])), cls.captions([value, make(cls)])]
            value._caption = "caption"
            row["captions"].append(cls.captions([value, make(cls)]))
            row["no_run"] = outcome(lambda: make(cls).bind_to_run(None, "x", 0))
            def missing(path): raise FileNotFoundError(path)
            owner = SimpleNamespace(get_entry=missing)
            row["decode_errors"] = [outcome(lambda v=v: cls.from_json(v, owner)) for v in [{}, {"path": "absent"}, None, []]]
        for text in ["plain", "  <html>x</html>  ", "<html><head></head><body>x</body></html>"]:
            for inject in [False, True]:
                value = w.Html(text, inject=inject)
                frozen = Path(value._path).read_text()
                value.inject_head()
                # CSS is intentionally self-contained in Foundry. Preserve
                # all other insertion/strip and freeze behavior in comparison.
                def skin(html):
                    return re.sub(r'<base target="_blank">(?:<style>.*?</style>|<link[^>]+/>)', '<STYLE>', html)
                out["html_inputs"].append({"frozen": skin(frozen), "after": skin(value.html), "unchanged_file": Path(value._path).read_text() == frozen})
        path = directory / "source.html"; path.write_text("<h1>source</h1>")
        for argument, kwargs in [(path, {}), (str(path), {"data_is_not_path": True}), (io.StringIO("abc"), {}), (io.BytesIO(b"abc"), {}), (None, {}), (12, {}), (Path("missing.html"), {})]:
            out["html_inputs"].append(outcome(lambda: w.Html(argument, inject=False, **kwargs).html))
        value = w.Html(path, inject=False)
        out["html_source"] = {"is_source": value._path == str(path), "temporary": value._is_tmp}
        run = ProbeRun(directory / "copy-html")
        value.bind_to_run(run, "html", 0)
        out["html_source"].update(source_exists=path.exists(), bound_bytes=Path(value._path).read_text())
        out["html_batches"] = [outcome(lambda seq=seq: w.Html.seq_to_json(seq, run, "x", 0)) for seq in [[], [value], [value, object()], None, iter([value])]]
        out["html_batch_directory"] = Path(run.dir, "media/html").is_dir()
# Keep the source hashes out of cross-runtime equality; the runner checks them.
print(json.dumps(out, sort_keys=True))
