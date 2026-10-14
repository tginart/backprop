"""Shared public serialization protocol for supported logging values.

Architecture: values expose type names and deterministic suffixes; Run binding
creates native descriptors through Encoder; artifact decoding discovers locally
defined subclasses once, using the pinned mutable registry cache. No descriptor can import arbitrary Python or execute logged content.
Main callers: media/Table APIs, Artifact codecs and W&B import aliases.
"""
from __future__ import annotations


class WBValue:
    _log_type = None
    _type_mapping = None

    def __init__(self):
        self._artifact_source = self._artifact_target = None

    def to_json(self, run_or_artifact):
        raise NotImplementedError

    def _set_artifact_source(self, artifact, name=None):
        assert getattr(self, "_artifact_source", None) is None, "Cannot update artifact_source"
        self._artifact_source, self._artifact_source_name = artifact, name

    def _set_artifact_target(self, artifact, name=None):
        assert getattr(self, "_artifact_target", None) is None, "Cannot update artifact_target"
        self._artifact_target, self._artifact_target_name = artifact, name

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        """Preserve the pinned abstract deserialization contract.

        Main callers: public value APIs and extension subclasses. Concrete
        types override this; a Node has no standalone artifact decoder upstream.
        """
        raise NotImplementedError

    @classmethod
    def with_suffix(cls, name, filetype="json"):
        suffix = cls._log_type + "." + filetype if cls._log_type is not None else filetype
        return name if name.endswith(suffix) else name + "." + suffix

    @staticmethod
    def type_mapping():
        """Discover local value classes with the pinned first-lookup cache.

        Main callers: artifact decoding and user-defined WBValue extensions.
        Registration executes no imported/logged code: only classes already in
        this process are eligible. Callers may mutate or reset the public cache,
        matching W&B. Internal capture handles use their portable Graph class.
        """
        if WBValue._type_mapping is None:
            WBValue._type_mapping = {}
            frontier, explored = [WBValue], set()
            while frontier:
                value_type = frontier.pop()
                explored.add(value_type)
                if value_type._log_type is not None and not value_type.__dict__.get("_registry_exclude", False):
                    WBValue._type_mapping[value_type._log_type] = value_type
                frontier.extend(child for child in value_type.__subclasses__() if child not in explored)
        return WBValue._type_mapping

    @staticmethod
    def init_from_json(json_obj, source_artifact):
        kind = json_obj["_type"]
        aliases = {"image": "image-file", "audio": "audio-file", "video": "video-file", "html": "html-file", "plotly": "plotly-file", "graph": "graph-file"}
        mapping = WBValue.type_mapping()
        cls = mapping.get(kind) or mapping.get(aliases.get(kind, kind))
        if cls is None:
            return None
        value = cls.from_json(json_obj, source_artifact)
        if type(value).__module__.startswith("backprop.") and getattr(value, "_artifact_source", None) is source_artifact:
            return value
        value._set_artifact_source(source_artifact)
        return value

    def __eq__(self, other):
        return id(self) == id(other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def to_data_array(self):
        """Keep the abstract primitive-array hook identical to the reference.

        Main callers: extensions and Table column conversion. Concrete values
        override this where the reference supplies a primitive representation.
        """
        raise NotImplementedError

    def _get_artifact_entry_ref_url(self):
        from .offline_artifacts import artifact_reference
        return artifact_reference(self)

    def _get_artifact_entry_latest_ref_url(self):
        from .offline_artifacts import latest_artifact_reference
        return latest_artifact_reference(self)

    @classmethod
    def captions(cls, media_items):
        values = list(media_items)
        return [getattr(value, "caption", None) for value in values] if getattr(values[0], "caption", None) is not None else False

    @staticmethod
    def path_is_reference(path):
        from .media_references import is_reference
        return is_reference(path)

    def file_is_set(self):
        return getattr(self, "path", None) is not None or isinstance(getattr(self, "data", None), bytes) or getattr(self, "_bound_json", None) is not None

    def is_bound(self):
        return getattr(self, "_bound_run", None) is not None

    def bind_to_run(self, run, key, step, id_=None, ignore_copy_err=None):
        if self.is_bound() and self._bound_run is not run:
            raise ValueError("Value is already bound to another run")
        self._bound_json = run._encoder().encode(self)
        self._bound_run = run
