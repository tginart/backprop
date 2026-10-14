"""554 paired native dtype cases against hash-pinned W&B0.30 source bodies.

Architecture: existing source fixture -> fresh reference/native environments ->
identical constructor/cast/add/equality calls -> compare returned type, semantic
schema, data changes and error classes. Failed additions must leave state intact.
Each subtest names its family, optionality and input, without successful-case spam.
Main callers: unittest discovery in both scientific and standard-library runtimes.

Fixture limits: media objects contain encoded byte samples; this tests dtype
algorithms, not image/audio/video decoding. The reference Image receives its
original _boxes/_masks/_classes attribute view. Type repr addresses and concrete
module names are not compared. Persistence, provider services and media encoders
have separate tests. The source fixture verifies file hashes before execution.
"""
from __future__ import annotations

import io
import unittest
import wave

import backprop as wandb
from backprop import table_native_types, table_types
from test_sdk_native_table_types import HASHES, PNG, SOURCE, source_adapters


CASES = {
    "fk": ("str", "key", "other_key", "none", "num", "bad"),
    "fi": ("num", "index", "other_index", "none", "str", "bad"),
    "pk": ("str", "key", "none", "num", "bad"),
    "classes": ("class_id", "class_bad", "none", "bool", "str"),
    "image": ("image", "none", "audio", "bad"),
    "audio": ("audio", "none", "html", "bad"),
    "html": ("html", "none", "image", "bad"),
    "video": ("video", "none", "image", "bad"),
    "table": ("table_value", "none", "str", "bad"),
    "image_const": ("image", "none", "bad"),
    "classes_const": ("classes", "none", "class_id"),
    "list_fi": ("nested_index", "nested_none", "none", "bad"),
    "dict_fk": ("dict_key", "dict_none", "none", "bad"),
    "list_image": ("images", "none", "bad"),
    "optional_fi": ("num", "index", "other_index", "none", "bad"),
    "infer": ("image", "audio", "html", "table_value", "classes", "key", "index", "none", "str", "num"),
}
EXPECTED_COUNTS = {"values": 444, "roles": 14, "equality": 96, "total": 554, "failed_additions": 86}
HAS_REFERENCE = all((SOURCE / path).is_file() for path in HASHES)


def audio_bytes():
    """Make a valid tiny WAV with standard-library tools, independent of NumPy."""
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8)
        stream.writeframes(b"\0\0" * 2)
    return output.getvalue()


class MatrixHarness:
    """Keep source/native type identities isolated while comparing their behavior.

    Main callers: NativeDtypeMatrix. Each operation gets fresh tables/media,
    so an earlier cast or reference promotion cannot affect a later case.
    """

    def __init__(self):
        self.reference_types, self.reference_table, self.reference_adapters = source_adapters()
        self.audio = audio_bytes()

    def environment(self, native):
        table, types, adapters = ((wandb.Table, table_types, table_native_types) if native else
                                 (self.reference_table, self.reference_types, self.reference_adapters))
        target = table(columns=["key"], data=[["a"], ["b"]])
        target.set_pk("key")
        other = table(columns=["key"], data=[["a"], ["b"]])
        other.set_pk("key")
        image = wandb.Image(PNG)
        if not native:
            # The source type algorithm reads fields with these names. Native
            # Image stores annotation metadata and has a _masks helper method.
            image._masks, image._boxes, image._classes = {}, {}, None
        return {"Table": table, "types": types, "adapters": adapters, "target": target, "other": other,
                "image": image, "audio": wandb.Audio(self.audio), "html": wandb.Html("<b>ok</b>"),
                "video": wandb.Video(b"\x1aE\xdf\xa3test", format="webm"),
                "classes": wandb.Classes([{"id": 1, "name": "one"}, {"id": 2, "name": "two"}])}

    def canonical(self, value, environment):
        """Compare semantic params while retaining relationships to actual targets."""
        if hasattr(value, "name") and hasattr(value, "params"):
            return {"type": value.name, "params": self.canonical(value.params, environment)}
        if isinstance(value, (self.reference_table, wandb.Table)):
            for name in ("target", "other", "current"):
                if value is environment.get(name):
                    return {"table": name}
            return {"table-data": self.canonical(value.data, environment),
                    "schema": self.canonical(value._column_types, environment)}
        if hasattr(value, "_table") and isinstance(value, (str, int)):
            return {"link": str(value) if isinstance(value, str) else int(value),
                    "table": self.canonical(value._table, environment), "column": getattr(value, "_col_name", None)}
        if isinstance(value, dict):
            return {str(key): self.canonical(item, environment) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.canonical(item, environment) for item in value]
        if isinstance(value, (set, frozenset)):
            return sorted((self.canonical(item, environment) for item in value), key=repr)
        if isinstance(value, wandb.Classes):
            return {"classes": value._class_set}
        if isinstance(value, (wandb.Image, wandb.Audio, wandb.Html, wandb.Video)):
            return {"media": value._log_type}
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        raise AssertionError(f"Matrix canonicalizer does not handle {type(value).__name__}")

    def snapshot(self, table, environment):
        # Primary-key cells point back to their owning table. Name that owner
        # explicitly instead of recursively expanding its rows again.
        environment = {**environment, "current": table}
        return {"data": self.canonical(table.data, environment),
                "schema": self.canonical(table._column_types, environment),
                "primary_key": table._pk_col,
                "foreign_keys": sorted(table._fk_cols)}

    @staticmethod
    def dtype(name, environment):
        adapters, types = environment["adapters"], environment["types"]
        factories = {
            "fk": lambda: adapters._ForeignKeyType(environment["target"], "key"),
            "fi": lambda: adapters._ForeignIndexType(environment["target"]),
            "pk": adapters._PrimaryKeyType,
            "classes": lambda: adapters._ClassesIdType(environment["classes"]),
            "image": lambda: wandb.Image, "audio": lambda: wandb.Audio,
            "html": lambda: wandb.Html, "video": lambda: wandb.Video,
            "table": lambda: environment["Table"], "image_const": lambda: environment["image"],
            "classes_const": lambda: environment["classes"],
            "list_fi": lambda: types.ListType(adapters._ForeignIndexType(environment["target"])),
            "dict_fk": lambda: types.TypedDictType({"v": adapters._ForeignKeyType(environment["target"], "key")}),
            "list_image": lambda: types.ListType(types.TypeRegistry.type_from_dtype(wandb.Image)),
            "optional_fi": lambda: types.OptionalType(adapters._ForeignIndexType(environment["target"])),
            "infer": lambda: None,
        }
        return factories[name]()

    @staticmethod
    def value(key, environment):
        factories = {
            "none": lambda: None, "bad": lambda: {"wrong": "value"}, "str": lambda: "a",
            "num": lambda: 0, "bool": lambda: True,
            "key": lambda: environment["target"].data[0][0],
            "other_key": lambda: environment["other"].data[0][0],
            "index": lambda: environment["target"].index_ref(0),
            "other_index": lambda: environment["other"].index_ref(0),
            "class_id": lambda: 1, "class_bad": lambda: 3,
            "nested_index": lambda: [environment["target"].index_ref(0)], "nested_none": lambda: [None],
            "dict_key": lambda: {"v": environment["target"].data[0][0]}, "dict_none": lambda: {"v": None},
            "images": lambda: [environment["image"]],
            "table_value": lambda: environment["Table"](columns=["x"], data=[[1]]),
        }
        return factories[key]() if key in factories else environment[key]

    def observe(self, native, family, optional, operation, keys):
        """Capture only SDK errors; a broken test serializer must fail the test.

        Main callers: constructor/cast/add matrix. Mutation snapshots are built
        outside exception handling, so test-helper errors cannot masquerade as
        matching SDK validation failures on both sides.
        """
        environment = self.environment(native)
        table_type, dtype = environment["Table"], self.dtype(family, environment)
        returned = None
        try:
            if operation == "construct":
                table = table_type(columns=["v"], data=[[self.value(key, environment)] for key in keys],
                                   dtype=dtype, optional=optional)
            elif operation == "cast":
                table = table_type(columns=["v"], data=[[self.value(key, environment)] for key in keys],
                                   allow_mixed_types=True)
                returned = table.cast("v", dtype, optional=optional)
            elif operation == "add":
                table = table_type(columns=["v"], data=[[self.value(keys[0], environment)]],
                                   dtype=dtype, optional=optional)
            else:
                raise AssertionError(f"Unknown operation: {operation}")
        except Exception as error:
            return {"error": type(error).__name__}
        if operation == "add":
            before = self.snapshot(table, environment)
            try:
                table.add_data(self.value(keys[1], environment))
            except Exception as error:
                after = self.snapshot(table, environment)
                return {"error": type(error).__name__, "atomic": before == after, "after": after}
        return {**self.snapshot(table, environment), "returned": self.canonical(returned, environment)}

    def role_case(self, native, family, optional, operation):
        environment = self.environment(native)
        table_type, dtype = environment["Table"], self.dtype(family, environment)
        try:
            if operation == "empty":
                table = table_type(columns=["v"], dtype=dtype, optional=optional)
                table.cast("v", dtype, optional=optional)
            elif operation == "repeat":
                table = table_type(columns=["v"], data=[[self.value(CASES[family][0], environment)]],
                                   dtype=dtype, optional=optional)
                table.cast("v", dtype, optional=optional)
            elif operation == "two_pk":
                key_type = environment["adapters"]._PrimaryKeyType
                table = table_type(columns=["a", "b"], dtype=[key_type(), key_type()], optional=optional)
            else:
                raise AssertionError(f"Unknown role operation: {operation}")
        except Exception as error:
            return {"error": type(error).__name__}
        return self.snapshot(table, environment)

    def equality_case(self, native, family, optional, variant):
        environment = self.environment(native)
        types = environment["types"]
        left, right = self.dtype(family, environment), self.dtype(family, environment)
        if family == "infer":
            left, right = types.UnknownType(), types.UnknownType()
        if variant == "different_reference" and family in {"fk", "fi", "optional_fi"}:
            right = self.dtype(family, {**environment, "target": environment["other"]})
        elif variant == "different_schema":
            right = types.StringType()
        left, right = types.TypeRegistry.type_from_dtype(left), types.TypeRegistry.type_from_dtype(right)
        if optional:
            left, right = types.OptionalType(left), types.OptionalType(right)
        try:
            return {"equal": bool(left == right), "unequal": bool(left != right)}
        except Exception as error:
            return {"error": type(error).__name__}


@unittest.skipUnless(HAS_REFERENCE, "Pinned W&B0.30 source snapshot is required for the differential matrix")
class NativeDtypeMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness = MatrixHarness()

    def test_constructor_cast_and_add_data_matrix(self):
        count, failed_additions = 0, 0
        self.assertEqual(len(CASES), 16)
        for family, keys in CASES.items():
            for optional in (False, True):
                for operation in ("construct", "cast", "add"):
                    inputs = [[keys[0], key] for key in keys] if operation == "add" else [[key] for key in keys]
                    for values in inputs:
                        with self.subTest(family=family, optional=optional, operation=operation, values=values):
                            reference, native = [self.harness.observe(side, family, optional, operation, values)
                                                 for side in (False, True)]
                            self.assertEqual(native, reference)
                            if "atomic" in native:
                                failed_additions += 1
                                self.assertTrue(native["atomic"], "A rejected addition mutated its committed table")
                        count += 1
        self.assertEqual(count, EXPECTED_COUNTS["values"])
        self.assertEqual(failed_additions, EXPECTED_COUNTS["failed_additions"])

    def test_empty_and_repeated_key_roles_matrix(self):
        cases = [(family, optional, operation) for family in ("pk", "fk", "fi")
                 for optional in (False, True) for operation in ("empty", "repeat")]
        cases += [("pk", optional, "two_pk") for optional in (False, True)]
        for family, optional, operation in cases:
            with self.subTest(family=family, optional=optional, operation=operation):
                reference, native = [self.harness.role_case(side, family, optional, operation) for side in (False, True)]
                self.assertEqual(native, reference)
        self.assertEqual(len(cases), EXPECTED_COUNTS["roles"])

    def test_type_equality_matrix(self):
        count = 0
        for family in CASES:
            for optional in (False, True):
                for variant in ("same", "different_schema", "different_reference"):
                    with self.subTest(family=family, optional=optional, variant=variant):
                        reference, native = [self.harness.equality_case(side, family, optional, variant)
                                             for side in (False, True)]
                        self.assertEqual(native, reference)
                    count += 1
        self.assertEqual(count, EXPECTED_COUNTS["equality"])
        self.assertEqual(EXPECTED_COUNTS["values"] + EXPECTED_COUNTS["roles"] + count, EXPECTED_COUNTS["total"])


if __name__ == "__main__":
    unittest.main()
