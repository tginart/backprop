"""Media adapters with lazy optional dependencies and explicit file formats.

Architecture: constructors normalize files/PIL/arrays/tensors into encoded bytes
and metadata; Encoder copies those bytes into the durable journal; the browser
receives ordinary Foundry file references. Raw media never creates a service.
NumPy/Pillow/MoviePy are loaded only for the input forms that require them.
Main callers: training code and data_types.Encoder.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import mimetypes
import numbers
from pathlib import Path
import struct
from types import ModuleType
import warnings
import wave

from .tables import _optional_module
from .values import WBValue
from .media_base import Media, BatchableMedia


def _array(value):
    if hasattr(value, "detach"):
        value = value.detach()
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return _optional_module("numpy", "Raw media encoding").asarray(value)


def _is_matplotlib(value):
    """Recognize both artist instances and the pyplot module without imports.

    Main callers: Image and Plotly dispatch. Python gives modules the type name
    builtins.module, so checking only type.__module__ misses pyplot inputs.
    """
    name = value.__name__ if isinstance(value, ModuleType) else type(value).__module__
    return name.startswith("matplotlib.")


def _figure(value):
    """Select an actual Figure before image inspection or interactive export.

    Main callers: Image and Plotly. pyplot.savefig can draw the current figure,
    but pyplot.axes is a function, so return gcf() for shared downstream checks.
    """
    if isinstance(value, ModuleType) and value.__name__ == "matplotlib.pyplot":
        return value.gcf()
    if hasattr(value, "savefig"):
        return value
    if hasattr(getattr(value, "figure", None), "savefig"):
        return value.figure
    raise TypeError("Expected a matplotlib figure, artist, or pyplot module")


class EncodedMedia(Media):
    """A file or already encoded byte payload, plus serializable display metadata."""

    kind = "file"

    def __init__(self, data_or_path, caption=None, **metadata):
        super().__init__(caption=caption)
        self.metadata = metadata
        self.path, self.data = None, None
        from .media_references import is_reference, initialize
        if self.kind in {"image", "audio"} and is_reference(data_or_path):
            initialize(self, data_or_path)
        elif isinstance(data_or_path, (str, Path)):
            self.path = Path(data_or_path)
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            self.metadata.setdefault("mime_type", mimetypes.guess_type(str(self.path))[0])
        elif isinstance(data_or_path, (bytes, bytearray)):
            self.data = bytes(data_or_path)
        elif hasattr(data_or_path, "read"):
            self.data = data_or_path.read()
            if not isinstance(self.data, bytes):
                raise TypeError("Encoded media file objects must return bytes")
        else:
            raise TypeError(f"{type(self).__name__} expects a file or encoded bytes")
        if not hasattr(self, "_reference_uri"):
            # Media equality and explicit binding use construction-time identity,
            # as upstream does. Journal uploads independently hash their frozen
            # bytes so allow_media_symlink never publishes a stale checksum.
            encoded = self._bytes()
            self._sha256, self._size = hashlib.sha256(encoded).hexdigest(), len(encoded)
            self._extension, self._is_tmp = None, self.path is None

    def _bytes(self):
        if hasattr(self, "_reference_uri"):
            raise ValueError("External media bytes must be retrieved through its artifact entry")
        return self.path.read_bytes() if self.path else self.data

    @staticmethod
    def path_is_reference(path):
        from .media_references import is_reference
        return is_reference(path)

    def file_is_set(self):
        return hasattr(self, "_reference_uri") or WBValue.file_is_set(self)

    def is_bound(self):
        return getattr(self, "_bound_run", None) is not None

    @classmethod
    def get_media_subdir(cls):
        return "media/" + {"image": "images", "audio": "audio", "video": "videos", "html": "html"}.get(cls.kind, cls.kind)

    def bind_to_run(self, run, key, step, id_=None, ignore_copy_err=None):
        if hasattr(self, "_reference_uri"):
            raise ValueError(f"{type(self).__name__} media created by a reference to external storage cannot currently be added to a run")
        assert self.file_is_set(), "bind_to_run called before _set_file"
        assert run is not None, "Argument passed to bind_to_run must be a run object"
        if self.path is None:
            import tempfile
            suffix = ".graph.json" if self.kind == "graph" else mimetypes.guess_extension(self.metadata.get("mime_type") or "") or "." + self.kind
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                temporary.write(self.data)
            self._set_file(temporary.name, is_tmp=True)
        elif not hasattr(self, "_sha256"):
            self._set_file(str(self.path))
        super().bind_to_run(run, key, step, id_=id_, ignore_copy_err=ignore_copy_err)
        encoder = run._encoder()
        self._bound_json = encoder.encode(self)
        encoder.commit()

    def to_json(self, run_or_artifact):
        from .artifacts import Artifact
        if isinstance(run_or_artifact, Artifact):
            from .object_codec import encode_object
            return encode_object(self, run_or_artifact)
        if not self.is_bound() or self._bound_run is not run_or_artifact:
            raise ValueError("Bind media to this run before serializing it")
        return dict(self._bound_json)

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        from .object_codec import decode_object
        value = dict(json_obj)
        value["_type"] = cls.kind
        return decode_object(value, source_artifact)


class _FileEncodedMedia(EncodedMedia):
    """Encoded audio/video with real files and the public Media lifecycle.

    Architecture: retain existing byte encoders -> materialize generated files
    at construction -> inherit copy/move/link and ownership rules -> publish via
    the ordinary journal. Encoder separately produces browser descriptors.
    Why this exists: Image/Graph still have their own deferred binding paths;
    audio/video public serializers must not depend on the native run encoder.
    """

    def __init__(self, data_or_path, caption=None, **metadata):
        super().__init__(data_or_path, caption=caption, **metadata)
        if hasattr(self, "_reference_uri"):
            self._is_tmp = False
        else:
            self._freeze_encoded_file()

    def _freeze_encoded_file(self):
        """Give generated bytes an owned file; preserve caller-owned paths.

        Main callers: construction and Video.encode. Keep data bytes available
        for native consumers while Media binding uses the real path and hash.
        """
        if self.path is None:
            from .media_files import freeze_file
            extension = self.metadata.get("format") if self.kind == "video" else "wav"
            self._set_file(freeze_file(self.data, "." + extension), is_tmp=True)

    file_is_set = Media.file_is_set
    is_bound = Media.is_bound
    path_is_reference = staticmethod(Media.path_is_reference)

    def bind_to_run(self, run, key, step, id_=None, ignore_copy_err=None):
        if hasattr(self, "_reference_uri"):
            raise ValueError(f"{type(self).__name__} media created by a reference to external storage cannot currently be added to a run")
        return Media.bind_to_run(self, run, key, step, id_, ignore_copy_err)

    def to_json(self, run_or_artifact):
        """Return public file fields; provider URIs become artifact references.

        Main callers: Audio/Video serializers. URI construction never performs
        I/O; adding a URI to an artifact uses its existing reference handler.
        Native Encoder keeps its separate cached browser reference descriptor.
        """
        from .artifacts import Artifact
        if hasattr(self, "_reference_uri") and isinstance(run_or_artifact, Artifact):
            import os
            name = run_or_artifact.get_added_local_path_name(self._path)
            if name is None:
                name = "/".join([self.get_media_subdir(), self._sha256[:20], os.path.basename(self._path)])
                run_or_artifact.add_reference(self._path, name=name)
            result = {"path": name, "sha256": self._sha256}
            if self.caption is not None:
                result["caption"] = self.caption
        else:
            result = Media.to_json(self, run_or_artifact)
        return {**result, "_type": self._log_type}


class Classes(Media):
    """Named class IDs usable in Image metadata and Table column schemas."""

    _backprop_dtype = True
    _log_type = "classes"

    def __init__(self, class_set):
        super().__init__()
        # Match the pinned value contract: retain the supplied sequence and
        # validate its entries; Encoder freezes it at the persistence boundary.
        for item in class_set:
            assert "id" in item and "name" in item
        self._class_set = class_set

    @classmethod
    def get_media_subdir(cls):
        """Retain the pinned abstract directory contract for Classes values."""
        return Media.get_media_subdir.__func__(cls)

    def get_type(self):
        from .table_native_types import _ClassesIdType
        return _ClassesIdType(self)

    def to_json(self, run_or_artifact):
        """Serialize labels with the inherited file lifecycle when an owner exists.

        Main callers: table class schemas, Artifact.add and explicit serializers.
        Ordinary class sets have no file, so binding them directly requires a
        subclass to supply a file and media directory, as in the pinned API.
        """
        result = super().to_json(run_or_artifact) if run_or_artifact is not None else {}
        result.update(_type="classes", class_set=self._class_set)
        return result

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls(json_obj.get("class_set"))

    def __eq__(self, other):
        return isinstance(other, Classes) and self._class_set == other._class_set


def _class_labels(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, numbers.Number) or not isinstance(label, str) for key, label in value.items()):
        raise TypeError("Class labels must map numeric IDs to strings")
    return {str(key): label for key, label in value.items()}


def _boxes(layers):
    if layers is None:
        return None
    if not isinstance(layers, dict):
        raise TypeError("Image boxes must map layer names to box annotations")
    return {str(name): (layer if isinstance(layer, BoundingBoxes2D) else
                       BoundingBoxes2D(layer, name))._native_json()
            for name, layer in layers.items()}


class BoundingBoxes2D(Media):
    """Box annotations with pinned file binding and a native inline descriptor.

    Public serializers follow the owner-specific W&B contract. Encoder uses the
    separate native descriptor to retain class labels in immutable Foundry data.
    """
    _log_type = "bounding-boxes"

    def __init__(self, val, key):
        from .annotation_values import prepare_boxes, write_boxes, required_module
        super().__init__()
        prepare_boxes(val)
        self.validate(val)
        path = write_boxes(val)
        self._set_file(str(path), is_tmp=True, extension=".boxes2D.json")
        self._val, self._key = val["box_data"], key
        if "class_labels" in val:
            self._class_labels = val["class_labels"]
        else:
            numpy = required_module("numpy", "Bounding box support requires numpy")
            classes = numpy.unique([box["class_id"] for box in self._val]).astype(numpy.int32).tolist()
            self._class_labels = {value: "class_" + str(value) for value in classes}

    @classmethod
    def type_name(cls):
        return "boxes2D"

    @classmethod
    def get_media_subdir(cls):
        import os
        return os.path.join("media", "metadata", cls.type_name())

    def validate(self, val):
        from .annotation_values import validate_boxes
        return validate_boxes(val)

    def bind_to_run(self, run, key, step, id_=None, ignore_copy_err=None):
        super().bind_to_run(run, key, step, id_=id_, ignore_copy_err=ignore_copy_err)
        run._add_singleton("bounding_box/class_labels", str(key) + "_wandb_delimeter_" + self._key, self._class_labels)

    def _native_json(self):
        """Preserve inline labels for image viewers and native artifact retrieval.

        Main callers: Encoder and Image annotation normalization. Public
        to_json(artifact) intentionally returns only the pinned box-data list.
        """
        return {"box_data": copy.deepcopy(self._val), "class_labels": _class_labels(self._class_labels)}

    def to_json(self, run_or_artifact):
        from .run import Run
        from .artifacts import Artifact
        if isinstance(run_or_artifact, Run):
            return {**super().to_json(run_or_artifact), "_type": self.type_name()}
        if isinstance(run_or_artifact, Artifact):
            return self._val
        raise TypeError("to_json accepts wandb_run.Run or wandb.Artifact")

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls({"box_data": json_obj}, "")


class Image(EncodedMedia, BatchableMedia):
    """Image files, PIL images, NumPy arrays, torch tensors, and figures."""

    kind = "image"
    _log_type = "image-file"
    MAX_DIMENSION = 65500
    MAX_ITEMS = 108

    def __init__(self, data_or_path, mode=None, caption=None, grouping=None, classes=None,
                 boxes=None, masks=None, file_type=None, **metadata):
        if set(metadata) - {"mime_type"}:
            raise TypeError(f"Unknown Image options: {', '.join(set(metadata) - {'mime_type'})}")
        self._image = None
        self._array = None
        self._public_annotations = {}
        source_format = data_or_path.format if isinstance(data_or_path, Image) else None
        source_is_path = isinstance(data_or_path, (str, Path))
        copied_provenance = {}
        if isinstance(data_or_path, Image):
            original = data_or_path
            data_or_path = getattr(original, "_reference_uri", None) or original._bytes()
            for attribute in ("_artifact_source", "_artifact_source_name", "_artifact_content_path",
                              "_artifact_target", "_artifact_target_name"):
                if hasattr(original, attribute):
                    copied_provenance[attribute] = getattr(original, attribute)
            # Pinned upstream Image-copy semantics preserve image metadata but
            # deliberately do not implicitly copy box or mask annotations.
            inherited = {key: value for key, value in original.metadata.items() if key not in {"boxes", "masks"}}
            inherited.update(metadata)
            metadata = inherited
            if caption is None:
                caption = original.caption
        elif not isinstance(data_or_path, (str, Path, bytes, bytearray)):
            pil = _optional_module("PIL.Image", "Image array/PIL/figure encoding")
            format_name = (file_type or "png").lower()
            if format_name not in {"png", "jpg", "jpeg", "bmp"}:
                raise ValueError("Image file_type must be png, jpg, jpeg, or bmp")
            if hasattr(data_or_path, "savefig") or _is_matplotlib(data_or_path):
                buffer = io.BytesIO()
                _figure(data_or_path).savefig(buffer, format=format_name)
                image = pil.open(buffer)
            elif isinstance(data_or_path, pil.Image):
                image = data_or_path.copy()
            else:
                tensor = type(data_or_path).__module__.startswith("torch")
                array = _array(data_or_path)
                if tensor and array.ndim == 3:
                    array = array.transpose(1, 2, 0)
                # Preserve singleton height/width dimensions; only a redundant
                # grayscale channel is removed before Pillow conversion.
                if array.ndim == 3 and array.shape[-1] == 1:
                    array = array[:, :, 0]
                if array.ndim not in {2, 3} or array.ndim == 3 and array.shape[-1] not in {1, 3, 4}:
                    raise ValueError(f"Unsupported image shape: {array.shape}")
                # Version 0.30.0 explicitly casts to uint8. It no longer scales
                # floating images from [0,1] or normalizes negative values.
                array = array.astype("uint8")
                self._array = array
                image = pil.fromarray(array)
            if mode:
                image = image.convert(mode)
            if format_name in {"jpg", "jpeg"} and image.mode == "RGBA":
                warnings.warn("JPEG does not support alpha; converting to RGB", RuntimeWarning, stacklevel=2)
                image = image.convert("RGB")
            self._image = image.copy()
            metadata.update(width=image.width, height=image.height,
                            mime_type="image/jpeg" if format_name in {"jpg", "jpeg"} else "image/" + format_name)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG" if format_name in {"jpg", "jpeg"} else format_name.upper())
            data_or_path = buffer.getvalue()
        super().__init__(data_or_path, caption=caption, **metadata)
        for attribute, value in copied_provenance.items():
            setattr(self, attribute, value)
        prefix = b"" if hasattr(self, "_reference_uri") else self._bytes()[:32]
        if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
            self.metadata["mime_type"] = "image/png"
            if len(prefix) >= 24:
                self.metadata["width"], self.metadata["height"] = struct.unpack(">II", prefix[16:24])
        elif prefix.startswith(b"\xff\xd8\xff"):
            self.metadata["mime_type"] = "image/jpeg"
        elif prefix.startswith(b"GIF8"):
            self.metadata["mime_type"] = "image/gif"
            if len(prefix) >= 10:
                self.metadata["width"], self.metadata["height"] = struct.unpack("<HH", prefix[6:10])
        elif prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
            self.metadata["mime_type"] = "image/webp"
        if grouping is not None:
            self.metadata["grouping"] = grouping
        if boxes is not None:
            self._public_annotations["boxes"] = {key: value if isinstance(value, BoundingBoxes2D)
                else BoundingBoxes2D(value, key) for key, value in boxes.items()}
            self.metadata["boxes"] = _boxes(self._public_annotations["boxes"])
            from .table_image_type import capture_annotation_labels
            self._dtype_box_labels = capture_annotation_labels(boxes)
        if masks is not None:
            self._public_annotations["masks"] = {key: value if isinstance(value, ImageMask)
                else ImageMask(value, key) for key, value in masks.items()}
            self.metadata["masks"] = self._masks(self._public_annotations["masks"])
            from .table_image_type import capture_annotation_labels
            self._dtype_mask_labels = capture_annotation_labels(masks)
        class_map = {}
        for family in ["boxes", "masks"]:
            for layer in self.metadata.get(family, {}).values():
                class_map.update({int(key) if key.lstrip("-").isdigit() else key: name for key, name in layer.get("class_labels", {}).items()})
        if classes is not None:
            classes = classes if isinstance(classes, Classes) else Classes(classes)
            class_map.update({item["id"]: item["name"] for item in classes._class_set})
        if class_map:
            self.metadata["classes"] = Classes([{"id": key, "name": value} for key, value in class_map.items()])
        self.format = source_format or (self.path.suffix.lstrip(".") if source_is_path and self.path else file_type) or (self.metadata.get("mime_type") or "image/png").split("/")[-1]
        if hasattr(self, "_reference_uri"):
            from urllib.parse import urlsplit
            self.format = urlsplit(self._reference_uri).path.split("/")[-1].split(".")[-1]
        elif self.path is None:
            from .media_files import freeze_file
            self._set_file(freeze_file(self.data, "." + self.format), is_tmp=True)

    file_is_set = Media.file_is_set
    is_bound = Media.is_bound
    path_is_reference = staticmethod(Media.path_is_reference)

    def bind_to_run(self, run, key, step, id_=None, ignore_copy_err=None):
        from .image_public import bind
        return bind(self, run, key, step, id_, ignore_copy_err)

    def to_json(self, run_or_artifact):
        from .image_public import serialize
        return serialize(self, run_or_artifact)

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        from .image_public import deserialize
        return deserialize(cls, json_obj, source_artifact)

    def _masks(self, masks):
        if not isinstance(masks, dict):
            raise TypeError("Image masks must map layer names to mask dictionaries")
        result = {}
        for name, layer in masks.items():
            mask = layer if isinstance(layer, ImageMask) else ImageMask(layer, name)
            result[str(name)] = {"mask": Image(mask._bytes()),
                                 "class_labels": dict(mask.metadata["class_labels"])}
        return result

    @property
    def image(self):
        if hasattr(self, "_reference_uri"):
            return None
        if self._image is None:
            self._image = _optional_module("PIL.Image", "Image.image").open(io.BytesIO(self._bytes()))
        return self._image

    def __eq__(self, other):
        """Compare the pinned image value contract used by literal dtypes.

        Main callers: ConstType assignment and Table equality. Pixel equality,
        dimensions, caption, grouping and classes define the value; individual
        annotation geometries are not additional equality constraints upstream.
        """
        if not isinstance(other, Image):
            return False
        if hasattr(self, "_reference_uri") and hasattr(other, "_reference_uri"):
            return self._reference_uri == other._reference_uri
        left, right = self.image, other.image
        if left is None or right is None:
            return False
        return (self.caption == other.caption
                and self.metadata.get("grouping") == other.metadata.get("grouping")
                and left.size == right.size
                and self.to_data_array() == other.to_data_array()
                and self.metadata.get("classes") == other.metadata.get("classes"))

    def to_data_array(self):
        image = self.image
        if image is None:
            return []
        pixels = list(image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata())
        return [pixels[index * image.width:(index + 1) * image.width] for index in range(image.height)]

    def guess_mode(self, data, file_type=None):
        channels = data.shape[0] if type(data).__module__.startswith("torch") else data.shape[-1]
        if data.ndim == 2 or channels == 1:
            return "L"
        if channels == 3:
            return "RGB"
        if channels == 4:
            if file_type in {"jpg", "jpeg"}:
                warnings.warn("JPEG does not support alpha; converting to RGB", RuntimeWarning, stacklevel=2)
                return "RGB"
            return "RGBA"
        raise ValueError(f"Unsupported image shape: {data.shape}")

    @classmethod
    def all_captions(cls, images):
        return cls.captions(images)

    @classmethod
    def all_masks(cls, images, run, run_key, step):
        from .image_public import collect
        return collect(images, run, "masks")

    @classmethod
    def all_boxes(cls, images, run, run_key, step):
        from .image_public import collect
        return collect(images, run, "boxes")

    @classmethod
    def seq_to_json(cls, seq, run, key, step):
        from .image_public import batch
        return batch(cls, seq, run, key, step)


class ImageMask(EncodedMedia):
    """PNG mask files and native descriptors sharing the pinned Media lifecycle."""
    kind = "image"
    _log_type = "mask"

    def __init__(self, val, key):
        from .annotation_values import is_torch_tensor, temporary_path, required_module
        Media.__init__(self)
        self.metadata = {"mime_type": "image/png", "class_labels": {}}
        if "path" in val:
            self._set_file(Path(val["path"]))
            # Preserve supplied labels on local-file masks. Pinned W&B drops
            # this metadata; the Foundry descriptor keeps the useful mapping.
            if "class_labels" in val:
                self.metadata["class_labels"] = _class_labels(val["class_labels"])
                self._val, self._key = val, key
        else:
            numpy = required_module("numpy", "Image mask support requires numpy")
            if is_torch_tensor(val["mask_data"]):
                val["mask_data"] = val["mask_data"].cpu().numpy()
            if "class_labels" not in val:
                classes = numpy.unique(val["mask_data"]).astype(numpy.int32).tolist()
                val["class_labels"] = {value: "class_" + str(value) for value in classes}
            self.validate(val)
            self._val, self._key = val, key
            path = temporary_path(".mask.png")
            pil = required_module("PIL.Image", 'wandb.Image needs the PIL package. To get it, run "pip install pillow".')
            pil.fromarray(val["mask_data"].astype(numpy.int8)).convert("L").save(path, transparency=None)
            self._set_file(str(path), is_tmp=True, extension=".mask.png")
            self.metadata["class_labels"] = _class_labels(val["class_labels"])

    @classmethod
    def type_name(cls):
        return "mask"

    @classmethod
    def get_media_subdir(cls):
        return "media/images/mask"

    @staticmethod
    def path_is_reference(path):
        return Media.path_is_reference(path)

    def validate(self, val):
        from .annotation_values import validate_mask
        return validate_mask(val)

    def bind_to_run(self, run, key, step, id_=None, ignore_copy_err=None):
        Media.bind_to_run(self, run, key, step, id_=id_, ignore_copy_err=ignore_copy_err)
        if hasattr(self, "_val") and "class_labels" in self._val:
            run._add_singleton("mask/class_labels", str(key) + "_wandb_delimeter_" + self._key, self._val["class_labels"])

    def to_json(self, run_or_artifact):
        from .run import Run
        from .artifacts import Artifact
        result = Media.to_json(self, run_or_artifact)
        if isinstance(run_or_artifact, Run):
            result["_type"] = self.type_name()
        elif not isinstance(run_or_artifact, Artifact):
            raise TypeError("to_json accepts wandb_run.Run or wandb.Artifact")
        return result

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls({"path": source_artifact.get_entry(json_obj["path"]).download()}, key="")


class Audio(_FileEncodedMedia, BatchableMedia):
    """Audio paths and raw mono/multichannel samples encoded as PCM16 WAV."""

    kind = "audio"
    _log_type = "audio-file"

    def __init__(self, data_or_path, sample_rate=None, caption=None):
        self._sample_rate, self._duration = sample_rate, None
        duration = None
        if not isinstance(data_or_path, (str, Path, bytes, bytearray)):
            if sample_rate is None or not isinstance(sample_rate, numbers.Integral) or sample_rate <= 0:
                raise ValueError("A positive integer sample_rate is required for raw audio")
            dtype = getattr(data_or_path, "dtype", None)
            integer_kind = getattr(dtype, "kind", None) in {"i", "u"}
            integer_width = getattr(dtype, "itemsize", 0)
            if integer_kind and (getattr(dtype, "kind", None) != "i" or integer_width not in {2, 4}):
                raise TypeError("Raw integer audio supports int16 or int32 arrays")
            values = data_or_path.tolist() if hasattr(data_or_path, "tolist") else list(data_or_path)
            channels = len(values[0]) if values and isinstance(values[0], (list, tuple)) else 1
            if channels < 1:
                raise ValueError("Raw audio needs at least one channel")
            frames = []
            for sample in values:
                row = list(sample) if isinstance(sample, (list, tuple)) else [sample]
                if len(row) != channels:
                    raise ValueError("Raw audio must have equal channel counts in each frame")
                for value in row:
                    if not isinstance(value, numbers.Real) or not math.isfinite(value):
                        raise ValueError("Audio samples must be finite numbers")
                    scaled = int(value) >> (16 if integer_width == 4 else 0) if integer_kind else math.floor(float(value) * 32768)
                    frames.append(max(-32768, min(32767, scaled)))
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as output:
                output.setnchannels(channels)
                output.setsampwidth(2)
                output.setframerate(int(sample_rate))
                output.writeframes(struct.pack("<" + "h" * len(frames), *frames))
            data_or_path = buffer.getvalue()
            duration = len(values) / float(sample_rate)
            self._duration = duration
        super().__init__(data_or_path, caption=caption, sample_rate=sample_rate, duration=duration)
        if not hasattr(self, "_reference_uri") and self._bytes().startswith(b"RIFF"):
            self.metadata["mime_type"] = "audio/wav"
            with wave.open(io.BytesIO(self._bytes()), "rb") as source:
                self.metadata.update(sample_rate=source.getframerate(), duration=source.getnframes() / source.getframerate(), channels=source.getnchannels())

    @classmethod
    def durations(cls, audio_list):
        return [audio._duration for audio in audio_list]

    @classmethod
    def sample_rates(cls, audio_list):
        return [audio._sample_rate for audio in audio_list]

    @classmethod
    def captions(cls, audio_list):
        captions = [audio._caption for audio in audio_list]
        return False if all(c is None for c in captions) else ["" if c is None else c for c in captions]

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        value = cls(source_artifact.get_entry(json_obj["path"]).download(), caption=json_obj["caption"])
        value._artifact_content_path = json_obj["path"]
        return value

    def to_json(self, run):
        return super().to_json(run)

    @classmethod
    def seq_to_json(cls, seq, run, key, step):
        seq = list(seq)
        Path(run.dir, "media", "audio").mkdir(parents=True, exist_ok=True)
        result = {"_type": "audio", "count": len(seq), "audio": [audio.to_json(run) for audio in seq]}
        for name, values in [("sampleRates", cls.sample_rates(seq)), ("durations", cls.durations(seq)), ("captions", cls.captions(seq))]:
            if values:
                result[name] = values
        return result

    def resolve_ref(self):
        """Return the original provider URI, or None for an artifact's own file.

        Main callers: explicit reference inspection. Native artifact decoding
        records the content entry separately from the object's JSON entry.
        Unattached local audio has no artifact to resolve, matching the pinned
        AttributeError contract. Foundry storage URLs are not provider refs.
        """
        if hasattr(self, "_reference_uri"):
            return self._reference_uri
        source = self._artifact_source
        if source is None:
            raise AttributeError("Audio has no source artifact")
        path = getattr(self, "_artifact_content_path", None)
        if path is None:
            path = source.get_added_local_path_name(self._path)
        if path is None:
            return None
        entry = source.manifest.get_entry_by_path(path)
        return entry.ref if entry is not None else None

    def __eq__(self, other):
        # Pinned Audio inspects reference paths before Media's type guard.
        # Table's list inference compares cells with None, making this ordering
        # observable even when callers never explicitly compare Audio objects.
        if self.path_is_reference(self._path) or self.path_is_reference(other._path):
            return self.resolve_ref() == other.resolve_ref() and self.caption == other.caption
        return super().__eq__(other) and self.caption == other.caption


class Video(_FileEncodedMedia, BatchableMedia):
    """Encoded video or [time,channels,height,width] arrays, optionally batched."""

    kind = "video"
    _log_type = "video-file"
    EXTS = ("gif", "mp4", "webm", "ogg")

    def __init__(self, data_or_path, caption=None, fps=None, format=None):
        metadata = {}
        self._raw_data = None
        if isinstance(data_or_path, (str, io.BytesIO)) and fps:
            import backprop
            backprop.termwarn("`fps` argument does not affect the frame rate of the video "
                              "when providing a file path or raw bytes.")
        if isinstance(data_or_path, (str, Path)):
            chosen = Path(data_or_path).suffix.lstrip(".").lower()
            if chosen not in self.EXTS:
                raise ValueError("wandb.Video accepts " + ", ".join(self.EXTS) + " formats")
        elif isinstance(data_or_path, (bytes, bytearray)) or hasattr(data_or_path, "read"):
            chosen = self._format(format, "raw bytes (an io object)")
        else:
            from .video_encoding import raw_video, encode_video
            chosen = self._format(format)
            fps = fps or 4
            self._raw_data = raw_video(data_or_path)
            data_or_path, metadata = encode_video(self._raw_data, chosen, fps)
        metadata.update(format=chosen, mime_type="image/gif" if chosen == "gif" else "video/" + chosen)
        super().__init__(data_or_path, caption=caption, **metadata)

    @classmethod
    def _format(cls, value, source_description="a numpy array"):
        if value is None:
            raise ValueError("wandb.Video requires a `format` argument when initializing "
                             f"with {source_description}.")
        if value not in cls.EXTS:
            raise ValueError("wandb.Video accepts " + ", ".join(cls.EXTS) + " formats")
        return value

    @staticmethod
    def _prepare_video(array):
        from .video_encoding import prepare_video
        return prepare_video(array)

    def to_json(self, run_or_artifact):
        result = super().to_json(run_or_artifact)
        for name in ["width", "height"]:
            if self.metadata.get(name) is not None:
                result[name] = self.metadata[name]
        return result

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls(source_artifact.get_entry(json_obj["path"]).download())

    @classmethod
    def seq_to_json(cls, seq, run, key, step):
        Path(run.dir, cls.get_media_subdir()).mkdir(parents=True, exist_ok=True)
        return {"_type": "videos", "count": len(seq), "videos": [value.to_json(run) for value in seq], "captions": cls.captions(seq)}

    def encode(self, fps=4):
        if self._raw_data is None:
            raise ValueError("Video.encode requires a video created from raw arrays")
        from .video_encoding import encode_video
        data, metadata = encode_video(self._raw_data, self.metadata["format"], fps)
        self.path, self.data = None, data
        self.metadata = {**self.metadata, **metadata}
        self._freeze_encoded_file()


class Html(BatchableMedia):
    """HTML with a frozen file and the shared public Media lifecycle.

    Constructor injection uses local CSS for Foundry. Calling inject_head later
    changes the public html string, as upstream does; the already frozen file
    remains unchanged. Logging and artifact encoding consume the file snapshot.
    """
    kind = "html"
    _log_type = "html-file"

    def __init__(self, data, inject=True, data_is_not_path=False):
        import os
        from .media_files import freeze_file
        super().__init__()
        self.metadata = {"mime_type": "text/html"}
        self.inject = inject
        data_is_path = (isinstance(data, (str, Path)) and os.path.isfile(data)
                        and os.path.splitext(data)[1] == ".html" and not data_is_not_path)
        if data_is_path:
            self.html = Path(data).read_text(encoding="utf-8")
        elif isinstance(data, str):
            self.html = data
        elif isinstance(data, bytes):
            # Native artifact decoding also accepts already encoded UTF-8 HTML.
            self.html = data.decode("utf-8")
        elif hasattr(data, "read"):
            if hasattr(data, "seek"):
                data.seek(0)
            self.html = data.read()
        else:
            raise ValueError("data must be a string or an io object")
        if inject:
            self.inject_head()
        if inject or not data_is_path:
            if not isinstance(self.html, str):
                raise TypeError(f"write() argument must be str, not {type(self.html).__name__}")
            path = freeze_file(self.html.encode("utf-8"), ".html")
            self._set_file(path, is_tmp=True)
        else:
            self._set_file(str(data), is_tmp=False)

    def _bytes(self):
        return Path(self._path).read_bytes()

    def inject_head(self):
        # Foundry's CSP blocks the upstream normalize.css network fetch.
        head = '<base target="_blank"><style>html{font-family:system-ui,sans-serif}body{margin:0}img,video{max-width:100%}button,input,select,textarea{font:inherit}</style>'
        if "<head>" in self.html:
            self.html = self.html.replace("<head>", "<head>" + head, 1)
        elif "<html>" in self.html:
            self.html = self.html.replace("<html>", "<html><head>" + head + "</head>", 1)
        else:
            self.html = head + self.html
        self.html = self.html.strip()

    @classmethod
    def get_media_subdir(cls):
        return "media/html"

    def to_json(self, run_or_artifact):
        return {**super().to_json(run_or_artifact), "_type": self._log_type}

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return cls(source_artifact.get_entry(json_obj["path"]).download(), inject=False)

    @classmethod
    def seq_to_json(cls, seq, run, key, step):
        Path(run.dir, cls.get_media_subdir()).mkdir(parents=True, exist_ok=True)
        return {"_type": "html", "count": len(seq), "html": [value.to_json(run) for value in seq]}


class Plotly(Media):
    """Frozen Plotly JSON with public file binding and native inline rendering."""

    kind = "plotly"
    _log_type = "plotly-file"
    def __init__(self, val):
        from .plotly_spec import prepare_matplotlib_conversion, snapshot_spec
        super().__init__()
        if isinstance(val, dict):
            self.spec = snapshot_spec(val)
            self._freeze_spec()
            return
        typename = type(val).__module__ + "." + type(val).__name__
        if not (typename.startswith("plotly.") and typename.endswith(".Figure")):
            if not _is_matplotlib(val):
                raise ValueError("Logged plots must be plotly figures, or matplotlib plots convertible to plotly via mpl_to_plotly")
            figure = _figure(val)
            if any(getattr(axis, "images", []) for axis in getattr(figure, "axes", [])):
                raise ValueError("Matplotlib figures containing images must be logged with Image")
            prepare_matplotlib_conversion()
            tools = _optional_module("plotly.tools", "Matplotlib-to-Plotly conversion")
            val = tools.mpl_to_plotly(figure)
        self.spec = snapshot_spec(val.to_plotly_json())
        self._freeze_spec()

    def _freeze_spec(self):
        """Give public binding an immutable file while native charts use spec.

        Main callers: construction and native artifact reconstruction. Native
        Encoder handles Plotly before generic Media so browser data stays inline.
        """
        from .media_files import freeze_file
        import pydantic_core
        self._set_file(freeze_file(pydantic_core.to_json(self.spec, inf_nan_mode="constants"), ".plotly.json"),
                       is_tmp=True, extension=".plotly.json")


    @classmethod
    def make_plot_media(cls, val):
        if _is_matplotlib(val):
            figure = _figure(val)
            if any(getattr(axis, "images", []) for axis in getattr(figure, "axes", [])):
                return Image(figure)
        return cls(val)

    @classmethod
    def get_media_subdir(cls):
        return "media/plotly"

    def to_json(self, run_or_artifact):
        return {**super().to_json(run_or_artifact), "_type": self._log_type}

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        """Read a stored Plotly specification using the artifact's file API.

        Main callers: explicit decoding and WBValue.init_from_json. The pinned
        inherited decoder passes a filename to a Figure-only constructor and
        fails. Backprop repairs that path by reading JSON, preserving useful
        Foundry artifact round trips. Native inline specs remain accepted.
        """
        if "spec" in json_obj:
            if source_artifact is not None:
                from .object_codec import decode_object
                return decode_object({**json_obj, "_type": "plotly"}, source_artifact)
            return cls(json_obj["spec"])
        path = source_artifact.get_entry(json_obj["path"]).download()
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))
