"""Summary protobufs -> validated portable metrics and media values.

Architecture: remember tag plugin metadata -> choose the active W&B 0.30.0
converter -> validate its tensor layout -> create ordinary Backprop values.
Scalars, histograms, images and PR curves follow the pinned Go reader; text,
audio and unlabelled numeric tensors are retained native extensions.
Main callers: tensorboard.values and reader.sync. Unsupported or malformed
summaries raise visibly instead of being discarded or mislabeled as scalars.
"""
from __future__ import annotations

import io
import math


def _tensor(value):
    from tensorboard.util.tensor_util import make_ndarray
    return make_ndarray(value.tensor)


def _histogram(counts, edges):
    """Match upstream's 512-bin reduction while preserving total bin weight.

    Main callers: tensor and legacy histogram decoding. TensorBoard may emit
    more bins than the portable Histogram class permits. Interpolated bin edges
    retain the old edge distribution, and split weights retain the total.
    """
    import backprop
    counts, edges = list(map(float, counts)), list(map(float, edges))
    if len(edges) != len(counts) + 1:
        raise ValueError("TensorBoard histogram needs counts and one extra edge")
    if any(not math.isfinite(item) for item in counts + edges):
        raise ValueError("TensorBoard histogram counts and edges must be finite")
    if any(item < 0 for item in counts) or any(a >= b for a, b in zip(edges, edges[1:])):
        raise ValueError("TensorBoard histogram needs nonnegative counts and increasing edges")
    limit = backprop.Histogram.MAX_LENGTH
    if len(counts) <= limit:
        return backprop.Histogram(np_histogram=(counts, edges))
    old_count = len(counts)
    new_edges = [edges[0]]
    for index in range(1, limit):
        old_index, remainder = divmod(index * old_count, limit)
        new_edges.append(edges[old_index] + (edges[old_index + 1] - edges[old_index]) * remainder / limit)
    new_edges.append(edges[-1])
    weights = [0.0] * limit
    old_index = 0
    for new_index in range(limit):
        while edges[old_index + 1] < new_edges[new_index + 1]:
            weights[new_index] += counts[old_index]
            old_index += 1
        fraction = (new_edges[new_index + 1] - edges[old_index]) / (edges[old_index + 1] - edges[old_index])
        weights[new_index] += fraction * counts[old_index]
        weights[min(new_index + 1, limit - 1)] += (1 - fraction) * counts[old_index]
        old_index += 1
    return backprop.Histogram(np_histogram=(weights, new_edges))


def _legacy_histogram(value):
    histogram = value.histo
    edges, counts = list(histogram.bucket_limit), list(histogram.bucket)
    if not edges or len(edges) != len(counts):
        raise ValueError("TensorBoard HistogramProto bucket limits must match counts")
    if counts[0] == 0:
        counts = counts[1:]
    elif histogram.min < edges[0]:
        edges = [histogram.min, *edges]
    else:
        raise ValueError("TensorBoard HistogramProto min must precede first nonempty bucket")
    return _histogram(counts, edges)


def _images(encoded, width=None, height=None):
    """Validate encoded files before constructing their immutable media values.

    Main callers: legacy and tensor images. Validation keeps malformed protobuf
    bytes from becoming apparently successful uploads that cannot be displayed.
    """
    import backprop
    from PIL import Image
    if not encoded:
        raise ValueError("TensorBoard images summary contains no encoded image")
    result = []
    image_format = None
    for item in encoded:
        data = bytes(item)
        with Image.open(io.BytesIO(data)) as image:
            if width is not None and image.width != width or height is not None and image.height != height:
                raise ValueError("TensorBoard image dimensions do not match its encoded file")
            if image_format is not None and image.format != image_format:
                raise ValueError("TensorBoard image batch must use one encoded format")
            image_format = image.format
            encoded_width, encoded_height = image.width, image.height
        media = backprop.Image(data)
        # TensorBoard supplies validated dimensions for all image encodings.
        # Native Image's lightweight byte-prefix path extracts PNG/GIF sizes
        # but cannot extract JPEG sizes without this already completed decode.
        media.metadata.update(width=encoded_width, height=encoded_height)
        result.append(media)
    return result


def values(summary, namespace="", plugins=None):
    """Decode one summary while preserving the source's remembered tag plugins.

    Main callers: reader.sync and existing direct integration users. Protobuf
    writers commonly omit metadata after the first occurrence of a tag, so the
    reader passes one dictionary across that source stream. Stateless callers
    can pass a dictionary to get the same behavior over several summaries.
    """
    import backprop
    plugins = {} if plugins is None else plugins
    result = {}
    for value in summary.value:
        if not value.tag:
            raise ValueError("TensorBoard summary tags cannot be empty")
        tag = namespace + "/" + value.tag if namespace else value.tag
        named_plugin = value.metadata.plugin_data.plugin_name
        if named_plugin:
            plugins[tag] = named_plugin
        plugin = named_plugin or plugins.get(tag, "")
        kind = value.WhichOneof("value")
        if kind == "simple_value" and plugin in {"", "scalars"}:
            result[tag] = value.simple_value
        elif kind == "histo" and plugin in {"", "histograms"}:
            result[tag] = _legacy_histogram(value)
        elif kind == "image" and plugin in {"", "images"}:
            result[tag] = _images([value.image.encoded_image_string], value.image.width, value.image.height)
        elif kind == "audio" and plugin in {"", "audio"}:
            result[tag] = backprop.Audio(value.audio.encoded_audio_string, sample_rate=value.audio.sample_rate)
        elif kind == "tensor":
            if plugin == "images":
                encoded = list(value.tensor.string_val)
                if len(encoded) < 3:
                    raise ValueError("TensorBoard images tensor needs dimensions and encoded images")
                try:
                    width, height = int(encoded[0]), int(encoded[1])
                except (TypeError, ValueError) as error:
                    raise ValueError("TensorBoard images tensor dimensions must be integers") from error
                if width <= 0 or height <= 0:
                    raise ValueError("TensorBoard images dimensions must be positive")
                result[tag] = _images(encoded[2:], width, height)
            elif plugin in {"", "scalars"}:
                array = _tensor(value)
                if array.dtype.kind not in {"b", "i", "u", "f", "c"}:
                    raise TypeError("TensorBoard scalar tensor must contain numeric values")
                if plugin == "scalars" and array.size != 1:
                    raise ValueError("TensorBoard scalars tensor must contain exactly one value")
                if plugin == "scalars":
                    # The active Go reader's Tensor.Scalar returns float64 for
                    # DT_FLOAT/DOUBLE/INT32/INT64. Preserve that conversion even
                    # above 2**53, where integer values round to float64. The
                    # unlabelled tensor extension retains native array types.
                    result[tag] = float(array.reshape(-1)[0].item())
                else:
                    result[tag] = array.reshape(-1)[0].item() if array.size == 1 else array.tolist()
            elif plugin == "histograms":
                array = _tensor(value)
                if array.ndim != 2 or array.shape[1] != 3 or not array.shape[0]:
                    raise ValueError("TensorBoard histograms tensor must have nonempty [bins, 3] shape")
                result[tag] = _histogram(array[:, 2], [*array[:, 0], array[-1, 1]])
            elif plugin == "pr_curves":
                array = _tensor(value)
                if array.ndim != 2 or array.shape[0] < 2:
                    raise ValueError("TensorBoard PR curves tensor must have at least two rows")
                rows = [[recall, precision] for recall, precision in zip(array[-1].tolist(), array[-2].tolist())]
                table = backprop.Table(columns=["recall", "precision"], data=rows)
                result[tag] = backprop.plot_table("wandb/line/v0", table,
                    {"x": "recall", "y": "precision"}, {"title": f"{tag} Precision v. Recall"})
            elif plugin == "text":
                result[tag] = [bytes(item).decode("utf-8") for item in _tensor(value).reshape(-1)]
            elif plugin == "audio":
                array = _tensor(value)
                if array.ndim != 2 or array.shape[1] != 2:
                    raise ValueError("TensorBoard audio tensor must have [clips, 2] shape")
                result[tag] = [backprop.Audio(bytes(item[0]), caption=bytes(item[1]).decode("utf-8")) for item in array]
            else:
                raise NotImplementedError(f"TensorBoard plugin {plugin!r} has no portable converter")
        else:
            raise NotImplementedError(f"TensorBoard summary kind {kind!r} with plugin {plugin!r} has no portable converter")
    return result
