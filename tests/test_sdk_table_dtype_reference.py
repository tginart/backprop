"""Compare literal Table schemas with unchanged pinned W&B type checking.

Architecture: load pinned TypeRegistry and concrete types without service imports
-> extract Table's constructor/cast/row-validation methods -> compare accepted
values and mutation outcomes against Backprop. The reference omits only media
binding/key wrapping, which these scalar tests never invoke. Source hashes guard
the exact 0.30.0 baseline; this is not a full upstream service integration test.
Main callers: unittest discovery in the existing standard-library/runtime suites.
"""
import ast
import hashlib
import importlib
from pathlib import Path
from types import SimpleNamespace
import unittest

import backprop
from backprop.tables import ColumnType


SOURCE = Path(__file__).resolve().parents[1] / ".reference/wandb/source/wandb/sdk/data_types"
HASHES = {"_dtypes.py": "932bbbbfb9ff348da195b787428b4ff210233d2a7d575944c741d5b571889b3e",
          "table.py": "652cc8b4a9d51ef78917a5ea213ac5ccb9af4fce704a2293c812a01d9aaa96bd"}


def source_tree(name):
    path = SOURCE / name
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != HASHES[name]:
        raise AssertionError(f"Unexpected pinned W&B source: {name}")
    return path, ast.parse(data)


def reference_types():
    """Execute pinned dtype logic, replacing only its optional import boundary."""
    path, tree = source_tree("_dtypes.py")
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and node.module == "wandb.util")]
    def optional(name):
        try:
            return importlib.import_module(name)
        except ImportError:
            return None
    ns = {"__name__": "wandb.sdk.data_types._dtypes", "get_module": optional,
          "_is_artifact_string": lambda value: False,
          "_is_artifact_version_weave_dict": lambda value: False,
          "is_numpy_array": lambda value: optional("numpy") is not None and isinstance(value, optional("numpy").ndarray)}
    exec(compile(tree, str(path), "exec"), ns)
    return SimpleNamespace(**ns)


def reference_table(dtypes):
    """Execute exact Table constructor and validation methods for scalar rows.

    Why this exists: directly invoking TypeRegistry would miss the constructor's
    whole-None inference rule. The no-op key wrapper is outside these scalar
    comparisons; decorators only implement logging lifecycle after validation.
    """
    path, tree = source_tree("table.py")
    source = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Table")
    names = {"__init__", "_validate_log_mode", "_init_from_list", "_assert_valid_columns",
             "_to_per_column", "_make_column_types", "cast", "add_data", "add_column", "_get_updated_result_type"}
    methods = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in methods:
        node.decorator_list = [item for item in node.decorator_list
                               if isinstance(item, ast.Name) and item.id == "staticmethod"]
    cls = ast.ClassDef(name="Table", bases=[ast.Name(id="Base", ctx=ast.Load())], keywords=[], body=methods, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls], type_ignores=[]))
    ns = {"Base": type("Base", (), {}), "_dtypes": dtypes, "TYPE_CHECKING": False,
          "_SUPPORTED_LOGGING_MODES": {None, "IMMUTABLE", "MUTABLE", "INCREMENTAL"},
          "util": SimpleNamespace(is_numpy_array=lambda value: False, is_pandas_data_frame=lambda value: False),
          "_PrimaryKeyType": type("PrimaryKey", (), {}), "_ForeignKeyType": type("ForeignKey", (), {}),
          "_ForeignIndexType": type("ForeignIndex", (), {}), "_TableLinkMixin": type("Link", (), {})}
    exec(compile(module, str(path), "exec"), ns)
    ns["Table"]._update_keys = lambda self, **kwargs: None
    return ns["Table"]


def outcome(operation):
    try:
        return None, operation()
    except Exception as error:
        return type(error), None


@unittest.skipUnless((SOURCE / "_dtypes.py").exists(), "Pinned W&B source snapshot required")
class LiteralDtypeReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.types = reference_types()
        cls.reference = reference_table(cls.types)
        assert cls.reference(columns=["value"], data=[[1]]).data == [[1]]

    def test_constructor_constants_match_reference(self):
        constants = ["int", "float", "number", "bool", "str", "string", "object", "any", "datetime", "ordinary", 0, 1, 1.5, True, False, None]
        values = [None, 0, 1, 1.0, 1.5, True, False, "int", "str", "ordinary", "other", float("nan")]
        for constant in constants:
            for optional in (False, True):
                for value in values:
                    with self.subTest(dtype=constant, optional=optional, value=value):
                        expected = outcome(lambda: self.reference(columns=["value"], data=[[value]], dtype=[constant], optional=optional))
                        actual = outcome(lambda: backprop.Table(columns=["value"], data=[[value]], dtype=[constant], optional=optional))
                        self.assertEqual(actual[0], expected[0])
                        if expected[0] is None:
                            self.assertEqual(actual[1].data, expected[1].data)

    def test_constructor_whole_none_infers_but_column_none_is_literal(self):
        for cls in (self.reference, backprop.Table):
            table = cls(columns=["value"], data=[[1]], dtype=None, optional=False)
            table.add_data(2)
            self.assertEqual(table.data, [[1], [2]])
            with self.assertRaises(TypeError):
                cls(columns=["value"], data=[[1]], dtype=[None], optional=False)
            self.assertEqual(cls(columns=["value"], data=[[None]], dtype=[None], optional=False).data, [[None]])

    def test_cast_constants_and_failed_cast_are_atomic(self):
        for constant in ("int", "any", 0, 1, 1.5, True, False, None):
            for optional in (False, True):
                for value in (None, 0, 1, 1.0, True, False, "int", "other"):
                    with self.subTest(dtype=constant, optional=optional, value=value):
                        tables = [cls(columns=["value"], data=[[value]]) for cls in (self.reference, backprop.Table)]
                        old_type = tables[1]._types[:]
                        old_generation = tables[1]._generation
                        results = [outcome(lambda table=table: table.cast("value", constant, optional)) for table in tables]
                        self.assertEqual(results[0][0], results[1][0])
                        self.assertEqual(tables[1].data, [[value]])
                        if results[1][0] is not None:
                            self.assertEqual(tables[1]._types, old_type)
                            self.assertEqual(tables[1]._generation, old_generation)

    def test_rejected_late_cell_keeps_all_prior_column_types_and_rows(self):
        table = backprop.Table(columns=["inferred", "constant"], dtype=[ColumnType("unknown", False), "int"], optional=False)
        before = (table._types[:], list(table.data), table._generation)
        with self.assertRaises(TypeError):
            table.add_data("would infer string", "incorrect")
        self.assertEqual((table._types, table.data, table._generation), before)
        table.add_data(7, "int")
        self.assertEqual(table.data, [[7, "int"]])

    def test_native_schema_tags_remain_separate_from_public_constants(self):
        numeric = ColumnType.from_json({"wb_type": "number", "optional": False})
        table = backprop.Table(columns=["number", "literal"], data=[[7, "number"]], dtype=[numeric, "number"], optional=False)
        with self.assertRaises(TypeError):
            table.add_data("number", 7)
        serialized = table._types[1].to_json()
        self.assertEqual(serialized["upstream_schema"]["params"]["val"], "number")
        self.assertEqual(ColumnType.from_json(serialized).assign("number"), table._types[1])

    def test_standard_upstream_types_and_static_unions_keep_exact_acceptance(self):
        from datetime import date, datetime
        types = self.types
        dtypes = [getattr(types, name) for name in ("AnyType", "NoneType", "StringType", "NumberType", "BooleanType", "TimestampType", "InvalidType")]
        dtypes += [dtype() for dtype in dtypes]
        dtypes += [types.ConstType("int"), types.ConstType(1), types.ConstType(None),
                   types.OptionalType(types.NumberType()), types.UnionType([str, types.ConstType(7)])]
        values = [None, 0, 1, 1.0, True, False, "int", "other", 7, 1j, float("nan"), float("inf"), date(2026, 1, 1), datetime(2026, 1, 1), [], {}]
        for dtype in dtypes:
            for optional in (False, True):
                for value in values:
                    with self.subTest(dtype=repr(dtype), optional=optional, value=repr(value)):
                        expected = outcome(lambda: self.reference(columns=["value"], data=[[value]], dtype=dtype, optional=optional))
                        actual = outcome(lambda: backprop.Table(columns=["value"], data=[[value]], dtype=dtype, optional=optional))
                        self.assertEqual(actual[0], expected[0])
                        if actual[0] is None:
                            restored = ColumnType.from_json(actual[1]._types[0].to_json())
                            self.assertEqual(restored.assign(value), restored)

    def test_upstream_schema_roundtrip_and_unsupported_handlers_are_explicit(self):
        types = self.types
        dtype = types.OptionalType(types.NumberType())
        before = dtype.to_json()
        table = backprop.Table(columns=["value"], data=[[1], [None]], dtype=dtype, optional=False)
        self.assertEqual(dtype.to_json(), before)
        restored = ColumnType.from_json(table._types[0].to_json())
        self.assertEqual(restored.assign(None), restored)
        self.assertEqual(restored.assign(2), restored)
        with self.assertRaises(TypeError):
            restored.assign("2")
        for supported in (types.UnknownType(), types.ListType(str), types.TypedDictType({"value": int}), types.NDArrayType([2])):
            with self.subTest(type=type(supported).__name__):
                backprop.Table(columns=["value"], dtype=supported)
        with self.assertRaisesRegex(TypeError, "constraints cannot be discarded"):
            ColumnType.from_json({"wb_type": "number", "upstream_schema": {"wb_type": "number", "params": {"minimum": 0}}})

    def test_upstream_and_literal_dtypes_survive_immutable_artifact_retrieval(self):
        import tempfile
        from backprop.transport import sync
        with tempfile.TemporaryDirectory() as directory:
            table = backprop.Table(columns=["number", "literal"], data=[[1, "int"], [None, "int"]],
                                   dtype=[self.types.OptionalType(self.types.NumberType()), "int"], optional=False)
            artifact = backprop.Artifact("dtype-reference", "dataset")
            artifact.add(table, "table")
            sink = backprop.MemorySink()
            run = backprop.Run(project="dtype-reference", dir=directory, mode="offline",
                               settings={"console": "off", "silent": True, "disable_git": True})
            run.log_artifact(artifact)
            run.finish()
            sync(run.sync_dir, sink=sink)
            result = list(backprop.Api._from_store(sink, project="dtype-reference").run(run.id).logged_artifacts())[0].get("table")
            self.assertEqual(result.data, table.data)
            result.add_data(2, "int")
            before = result.data[:]
            with self.assertRaises(TypeError):
                result.add_data(3, 7)
            self.assertEqual(result.data, before)


if __name__ == "__main__":
    unittest.main()
