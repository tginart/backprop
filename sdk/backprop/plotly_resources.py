"""Freeze finite Plotly inputs into the existing run or artifact file store.

Architecture: inspect only Plotly resource fields -> bounded HTTP(S) reads ->
cache immutable bytes on the Plotly value -> stage those bytes with ordinary
file descriptors. The original figure stays unchanged. Dynamic tile, glyph and
sprite endpoints are deliberately left to the viewer's explicit origin policy.
Main callers: Encoder and artifact object decoding. No Foundry credential is
used for external reads, and replay never downloads an original URL again.
"""
from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_RESOURCE_BYTES = 32 * 1024 * 1024
MAX_FIGURE_BYTES = 128 * 1024 * 1024
MAX_RESOURCES = 256
RESOURCE_TIMEOUT = 15


def _remote(value):
    return isinstance(value, str) and urllib.parse.urlsplit(value).scheme.lower() in {"http", "https"}


def _style_fields(style, prefix):
    """Visit finite style sources without enumerating dynamic tile pyramids."""
    if not isinstance(style, dict):
        return
    for name, source in style.get("sources", {}).items():
        if not isinstance(source, dict):
            continue
        field, kind = ("data", "json") if source.get("type") == "geojson" else ("url", "image")
        if source.get("type") in {"geojson", "image"} and _remote(source.get(field)):
            yield prefix + ["sources", name, field], kind, source[field], False


def _layout_fields(layout, prefix):
    if not isinstance(layout, dict):
        return
    for index, image in enumerate(layout.get("images", [])):
        if isinstance(image, dict) and _remote(image.get("source")):
            yield prefix + ["images", index, "source"], "image", image["source"], False
    for name, subplot in layout.items():
        if not (name.startswith("map") and name.rstrip("0123456789") in {"map", "mapbox"} and isinstance(subplot, dict)):
            continue
        style = subplot.get("style")
        if _remote(style):
            yield prefix + [name, "style"], "json", style, True
        elif isinstance(style, dict):
            yield from _style_fields(style, prefix + [name, "style"])
        for index, layer in enumerate(subplot.get("layers", [])):
            if not isinstance(layer, dict):
                continue
            kind = {"geojson": "json", "image": "image"}.get(layer.get("sourcetype", "geojson"))
            if kind and _remote(layer.get("source")):
                yield prefix + [name, "layers", index, "source"], kind, layer["source"], False


def _trace_fields(traces, prefix):
    if not isinstance(traces, list):
        return
    for index, trace in enumerate(traces):
        if not isinstance(trace, dict):
            continue
        if trace.get("type") == "image" and _remote(trace.get("source")):
            yield prefix + [index, "source"], "image", trace["source"], False
        if trace.get("type") in {"choropleth", "choroplethmap", "choroplethmapbox", "scattergeo", "scattermap", "scattermapbox"} and _remote(trace.get("geojson")):
            yield prefix + [index, "geojson"], "json", trace["geojson"], False


def _animation_frames(spec):
    """Visit named frames and inline frames at their exact saved argument paths.

    Main caller: resource_fields. Named selections reuse the ordinary frame
    entries; only object selections contain additional resources to freeze.
    Buttons can store both args and args2, while sliders store args.
    """
    for index, frame in enumerate(spec.get("frames", [])):
        if isinstance(frame, dict):
            yield ["frames", index], frame
    layout = spec.get("layout", {})
    for collection, members in (("updatemenus", "buttons"), ("sliders", "steps")):
        for index, group in enumerate(layout.get(collection, [])):
            for offset, control in enumerate(group.get(members, [])):
                if control.get("method") != "animate":
                    continue
                for field in ("args", "args2"):
                    args = control.get(field)
                    if not isinstance(args, list) or not args:
                        continue
                    selection = args[0]
                    prefix = ["layout", collection, index, members, offset, field, 0]
                    if isinstance(selection, dict):
                        yield prefix, selection
                    elif isinstance(selection, list):
                        for item, frame in enumerate(selection):
                            if isinstance(frame, dict):
                                yield prefix + [item], frame


def resource_fields(spec):
    """Yield exact paths; unrelated customdata/source strings are user data.

    Main callers: capture. Template and animation defaults have the same fields
    as ordinary figures, but template trace defaults are grouped by trace type.
    """
    yield from _trace_fields(spec.get("data", []), ["data"])
    yield from _layout_fields(spec.get("layout", {}), ["layout"])
    template = spec.get("layout", {}).get("template", {})
    if isinstance(template, dict):
        yield from _layout_fields(template.get("layout", {}), ["layout", "template", "layout"])
        defaults = template.get("data", {})
        if isinstance(defaults, dict):
            for name, traces in defaults.items():
                normalized = [{"type": name, **trace} if isinstance(trace, dict) else trace for trace in traces] if isinstance(traces, list) else []
                yield from _trace_fields(normalized, ["layout", "template", "data", name])
    for prefix, frame in _animation_frames(spec):
        traces = []
        indices = frame.get("traces", [])
        for offset, trace in enumerate(frame.get("data", [])):
            parent_index = indices[offset] if offset < len(indices) else offset
            parents = spec.get("data", [])
            parent = parents[parent_index] if isinstance(parent_index, int) and 0 <= parent_index < len(parents) else {}
            traces.append({"type": parent.get("type"), **trace} if isinstance(trace, dict) and isinstance(parent, dict) else trace)
        yield from _trace_fields(traces, prefix + ["data"])
        yield from _layout_fields(frame.get("layout", {}), prefix + ["layout"])


class _Redirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _remote(newurl):
            raise ValueError("Plotly resource redirect must use HTTP or HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read(url, kind):
    """Read a supplied resource with bounded bytes and sanitized failures.

    Main callers: capture. A fresh opener has no application token or cookie
    jar. URLs can be signed input URLs, so failures must not reproduce them.
    """
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Backprop-Plotly/1"})
        deadline = time.monotonic() + RESOURCE_TIMEOUT
        with urllib.request.build_opener(_Redirects()).open(request, timeout=RESOURCE_TIMEOUT) as response:
            chunks, size = [], 0
            while size <= MAX_RESOURCE_BYTES:
                if time.monotonic() >= deadline:
                    raise ValueError("Plotly resource capture timed out")
                # read1 returns after an available socket read; a slow trickle
                # cannot reset the overall capture deadline indefinitely.
                chunk = response.read1(min(64 * 1024, MAX_RESOURCE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            content = b"".join(chunks)
            mime = response.headers.get_content_type()
            final_url = response.geturl()
        if len(content) > MAX_RESOURCE_BYTES:
            raise ValueError("Plotly resource exceeds the 32 MiB per-file limit")
        if kind == "json":
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("Plotly JSON resource must contain an object")
            mime = "application/json"
        else:
            parsed = None
            if not mime.startswith("image/"):
                mime = mimetypes.guess_type(urllib.parse.urlsplit(final_url).path)[0] or "application/octet-stream"
        return content, mime, parsed, final_url
    except Exception as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        if isinstance(error, ValueError) and str(error).startswith("Plotly"):
            raise
        raise ValueError(f"Plotly resource could not be captured ({type(error).__name__})") from None


def capture(plotly):
    """Capture once per value, including nested finite map-style resources.

    Main callers: encode_resources. The cache contains bytes, never store-specific
    descriptors, so the same value can safely move between runs and artifacts.
    A failed capture publishes nothing and never installs a partial cache.
    """
    if hasattr(plotly, "_resource_snapshots"):
        return plotly._resource_snapshots
    by_url, total, count = {}, 0, 0
    def one(path, kind, url, style=False):
        nonlocal total, count
        count += 1
        if count > MAX_RESOURCES:
            raise ValueError("Plotly figure exceeds the 256 resource limit")
        if (url, kind) not in by_url:
            read = _read(url, kind)
            total += len(read[0])
            if total > MAX_FIGURE_BYTES:
                raise ValueError("Plotly figure resources exceed 128 MiB")
            by_url[url, kind] = read
        data, mime, parsed, final_url = by_url[url, kind]
        result = {"path": path, "kind": kind, "data": data, "mime_type": mime}
        if style:
            nested = []
            # Style-relative GeoJSON/image URLs use the final redirected style
            # URL as their base. Dynamic source URL templates stay unchanged.
            normalized = copy.deepcopy(parsed)
            for source in normalized.get("sources", {}).values():
                if isinstance(source, dict):
                    field = "data" if source.get("type") == "geojson" else "url"
                    raw = source.get(field)
                    if source.get("type") in {"geojson", "image"} and isinstance(raw, str):
                        source[field] = urllib.parse.urljoin(final_url, raw)
            for inner_path, inner_kind, inner_url, _ in _style_fields(normalized, []):
                nested.append(one(inner_path, inner_kind, inner_url))
            result["resources"] = nested
            result["base_url"] = final_url
        return result
    result = [one(*item) for item in resource_fields(plotly.spec)]
    plotly._resource_snapshots = result
    return result


def encode_resources(plotly, stage_file):
    """Stage frozen bytes in the current owner and return portable references."""
    def one(value):
        extension = ".json" if value["kind"] == "json" else mimetypes.guess_extension(value["mime_type"]) or ".bin"
        name = hashlib.sha256(value["data"]).hexdigest() + extension
        reference = stage_file(None, data=value["data"], name=name)
        result = {"path": list(value["path"]), "kind": value["kind"],
                  "file": {**reference, "mime_type": value["mime_type"]}}
        if "resources" in value:
            result.update(resources=[one(item) for item in value["resources"]], base_url=value["base_url"])
        return result
    return [one(item) for item in capture(plotly)]


def restore_resources(plotly, resources, artifact):
    """Restore verified artifact bytes; original URLs are never re-requested.

    Main callers: object_codec.decode_object. This is the inverse owner boundary
    of encode_resources, not a second interpretation of the Plotly figure.
    """
    def one(value):
        file = value["file"]
        data = artifact.get_entry(file.get("artifact_path") or file["path"])._read_verified_bytes()
        result = {"path": list(value["path"]), "kind": value["kind"], "data": data,
                  "mime_type": file.get("mime_type", "application/octet-stream")}
        if "resources" in value:
            result.update(resources=[one(item) for item in value["resources"]], base_url=value["base_url"])
        return result
    plotly._resource_snapshots = [one(item) for item in resources]
    return plotly
