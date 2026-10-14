"""Pinned image annotation type inference over native Image metadata.

Architecture: normalize native annotation metadata to the source attribute view
-> run unchanged image type construction/merging -> retain declarative labels,
layer IDs and score keys. No image file is decoded by type checking.
Main callers: TypeRegistry inference and Table casts to the Image class.

Adapted from W&B 0.30.0, commit e5ec9dc9236d1a4fbb9fe7c0795bbccdff1e0e77.
MIT License

Copyright (c) 2021 Weights and Biases, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

from types import SimpleNamespace
import copy
from . import table_types as _dtypes
from .media import Image


def capture_annotation_labels(layers):
    """Retain original numeric keys before native JSON metadata stringifies them."""
    result = {}
    for key, layer in (layers or {}).items():
        if isinstance(layer, dict):
            labels = layer.get("class_labels")
        elif hasattr(layer, "_class_labels"):
            labels = layer._class_labels
        else:
            labels = getattr(layer, "_val", {}).get("class_labels")
        if labels is not None:
            result[str(key)] = copy.deepcopy(labels)
    return result


def _numeric_labels(labels):
    """Restore numeric annotation IDs from native JSON object keys."""
    result = {}
    for key, label in labels.items():
        if isinstance(key, str):
            try:
                key = int(key)
            except ValueError:
                try:
                    key = float(key)
                except ValueError:
                    pass
        result[key] = label
    return result


def _image_state(image):
    """Build the source view while retaining original in-memory label keys.

    Main callers: _ImageFileType.from_obj. Native storage uses string keys, but
    original numeric IDs are kept separately until persistence for exact params.
    """
    metadata = image.metadata
    boxes = {}
    for key, layer in metadata.get("boxes", {}).items():
        labels = getattr(image, "_dtype_box_labels", {}).get(key, _numeric_labels(layer["class_labels"]))
        boxes[key] = SimpleNamespace(_class_labels=labels, _val=layer["box_data"])
    masks = {}
    for key, layer in metadata.get("masks", {}).items():
        labels = getattr(image, "_dtype_mask_labels", {}).get(key, _numeric_labels(layer["class_labels"]))
        masks[key] = SimpleNamespace(_val={"class_labels": labels})
    return SimpleNamespace(_boxes=boxes, _masks=masks, _classes=metadata.get("classes"))


class _ImageFileType(_dtypes.Type):
    name = "image-file"
    legacy_names = ["wandb.Image"]
    types = [Image]

    def __init__(
        self,
        box_layers=None,
        box_score_keys=None,
        mask_layers=None,
        box_class_maps=None,
        mask_class_maps=None,
        class_map=None,
        **kwargs,
    ):
        box_layers = box_layers or {}
        box_score_keys = box_score_keys or []
        mask_layers = mask_layers or {}
        box_class_maps = box_class_maps or {}
        mask_class_maps = mask_class_maps or {}
        class_map = class_map or {}

        if isinstance(box_layers, _dtypes.ConstType):
            box_layers = box_layers._params["val"]
        if not isinstance(box_layers, dict):
            raise TypeError("box_layers must be a dict")
        else:
            box_layers = _dtypes.ConstType(
                {layer_key: set(box_layers[layer_key]) for layer_key in box_layers}
            )

        if isinstance(mask_layers, _dtypes.ConstType):
            mask_layers = mask_layers._params["val"]
        if not isinstance(mask_layers, dict):
            raise TypeError("mask_layers must be a dict")
        else:
            mask_layers = _dtypes.ConstType(
                {layer_key: set(mask_layers[layer_key]) for layer_key in mask_layers}
            )

        if isinstance(box_score_keys, _dtypes.ConstType):
            box_score_keys = box_score_keys._params["val"]
        if not isinstance(box_score_keys, list) and not isinstance(box_score_keys, set):
            raise TypeError("box_score_keys must be a list or a set")
        else:
            box_score_keys = _dtypes.ConstType(set(box_score_keys))

        if isinstance(box_class_maps, _dtypes.ConstType):
            box_class_maps = box_class_maps._params["val"]
        if not isinstance(box_class_maps, dict):
            raise TypeError("box_class_maps must be a dict")
        else:
            box_class_maps = _dtypes.ConstType(
                {
                    str(box_key): {
                        str(class_id): name
                        for class_id, name in box_class_maps[box_key].items()
                    }
                    for box_key in box_class_maps
                }
            )

        if isinstance(mask_class_maps, _dtypes.ConstType):
            mask_class_maps = mask_class_maps._params["val"]
        if not isinstance(mask_class_maps, dict):
            raise TypeError("mask_class_maps must be a dict")
        else:
            mask_class_maps = _dtypes.ConstType(
                {
                    str(mask_key): {
                        str(class_id): name
                        for class_id, name in mask_class_maps[mask_key].items()
                    }
                    for mask_key in mask_class_maps
                }
            )

        if isinstance(class_map, _dtypes.ConstType):
            class_map = class_map._params["val"]
        if not isinstance(class_map, dict):
            raise TypeError("class_map must be a dict")
        else:
            class_map = _dtypes.ConstType(class_map)

        self.params.update(
            {
                "box_layers": box_layers,
                "box_score_keys": box_score_keys,
                "mask_layers": mask_layers,
                "box_class_maps": box_class_maps,
                "mask_class_maps": mask_class_maps,
                "class_map": class_map,
            }
        )

    def assign_type(self, wb_type=None):
        if isinstance(wb_type, _ImageFileType):
            box_layers_self = self.params["box_layers"].params["val"] or {}
            box_score_keys_self = self.params["box_score_keys"].params["val"] or []
            mask_layers_self = self.params["mask_layers"].params["val"] or {}
            class_map_self = self.params["class_map"].params["val"] or {}
            box_class_maps_self = (
                self.params.get("box_class_maps").params["val"]
                if "box_class_maps" in self.params
                else {}
            ) or {}
            mask_class_maps_self = (
                self.params.get("mask_class_maps").params["val"]
                if "mask_class_maps" in self.params
                else {}
            ) or {}

            box_layers_other = wb_type.params["box_layers"].params["val"] or {}
            box_score_keys_other = wb_type.params["box_score_keys"].params["val"] or []
            mask_layers_other = wb_type.params["mask_layers"].params["val"] or {}
            class_map_other = wb_type.params["class_map"].params["val"] or {}
            box_class_maps_other = (
                wb_type.params.get("box_class_maps").params["val"]
                if "box_class_maps" in wb_type.params
                else {}
            ) or {}
            mask_class_maps_other = (
                wb_type.params.get("mask_class_maps").params["val"]
                if "mask_class_maps" in wb_type.params
                else {}
            ) or {}

            # Merge the class_ids from each set of box_layers
            box_layers = {
                str(key): set(
                    list(box_layers_self.get(key, []))
                    + list(box_layers_other.get(key, []))
                )
                for key in set(
                    list(box_layers_self.keys()) + list(box_layers_other.keys())
                )
            }

            # Merge the class_ids from each set of mask_layers
            mask_layers = {
                str(key): set(
                    list(mask_layers_self.get(key, []))
                    + list(mask_layers_other.get(key, []))
                )
                for key in set(
                    list(mask_layers_self.keys()) + list(mask_layers_other.keys())
                )
            }

            # Merge the box score keys
            box_score_keys = set(list(box_score_keys_self) + list(box_score_keys_other))

            # Merge the class_map
            class_map = {
                str(key): class_map_self.get(key, class_map_other.get(key, None))
                for key in set(
                    list(class_map_self.keys()) + list(class_map_other.keys())
                )
            }

            def _merge_per_key_class_maps(self_map: dict, other_map: dict) -> dict:
                # Union of keys; within a key, union of class IDs, preferring
                # self's name on collision (mirrors `class_map` above).
                merged: dict = {}
                for key in self_map.keys() | other_map.keys():
                    labels = {**other_map.get(key, {}), **self_map.get(key, {})}
                    merged[str(key)] = {
                        str(class_id): name for class_id, name in labels.items()
                    }
                return merged

            box_class_maps = _merge_per_key_class_maps(
                box_class_maps_self, box_class_maps_other
            )
            mask_class_maps = _merge_per_key_class_maps(
                mask_class_maps_self, mask_class_maps_other
            )

            return _ImageFileType(
                box_layers,
                box_score_keys,
                mask_layers,
                box_class_maps,
                mask_class_maps,
                class_map,
            )

        return _dtypes.InvalidType()

    @classmethod
    def from_obj(cls, py_obj):
        if not isinstance(py_obj, Image):
            raise TypeError("py_obj must be a wandb.Image")
        else:
            py_obj = _image_state(py_obj)
            if hasattr(py_obj, "_boxes") and py_obj._boxes:
                box_layers = {
                    str(key): set(py_obj._boxes[key]._class_labels.keys())
                    for key in py_obj._boxes
                }
                box_score_keys = {
                    key
                    for val in py_obj._boxes.values()
                    for box in val._val
                    for key in box.get("scores", {})
                }
                box_class_maps = {
                    str(key): {
                        str(class_id): name
                        for class_id, name in py_obj._boxes[key]._class_labels.items()
                    }
                    for key in py_obj._boxes
                }
            else:
                box_layers = {}
                box_score_keys = set()
                box_class_maps = {}

            if hasattr(py_obj, "_masks") and py_obj._masks:
                mask_layers = {
                    str(key): set(
                        py_obj._masks[key]._val["class_labels"].keys()
                        if hasattr(py_obj._masks[key], "_val")
                        else []
                    )
                    for key in py_obj._masks
                }
                mask_class_maps = {
                    str(key): {
                        str(class_id): name
                        for class_id, name in py_obj._masks[key]
                        ._val["class_labels"]
                        .items()
                    }
                    for key in py_obj._masks
                    if hasattr(py_obj._masks[key], "_val")
                    and "class_labels" in py_obj._masks[key]._val
                }
            else:
                mask_layers = {}
                mask_class_maps = {}

            if hasattr(py_obj, "_classes") and py_obj._classes:
                class_set = {
                    str(item["id"]): item["name"] for item in py_obj._classes._class_set
                }
            else:
                class_set = {}

            return cls(
                box_layers,
                box_score_keys,
                mask_layers,
                box_class_maps,
                mask_class_maps,
                class_set,
            )
