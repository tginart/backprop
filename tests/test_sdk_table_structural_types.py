"""Structural dtype contracts against pinned W&B's pure Python algorithms.

Architecture: execute the hash-verified reference machinery -> apply the same
ordered values/casts -> compare acceptance, resulting constraints and public
return objects. Separate immutable-artifact tests cover native serialization,
and custom handlers prove copy-on-write validation and explicit registration.
The reference Table omits media binding and remote service behavior. Its empty
NDArray list-axis loop is deliberately not executed because it never terminates.
Main callers: unittest discovery in existing stdlib and scientific runtimes.
"""
import ast
import copy
import importlib
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys

import backprop
from backprop import table_types as native
from backprop.tables import ColumnType
from backprop.transport import sync
from test_sdk_table_dtype_reference import SOURCE, reference_types, reference_table, source_tree, outcome


def custom_type(types):
    """Make the same stateful declared handler against either Type identity."""
    class CountedInteger(types.Type):
        name = "backprop_test_counted_integer"

        def __init__(self, minimum=0, seen=None):
            self.params.update(minimum=minimum, seen=list(seen or []))

        def assign(self, value=None):
            # Mutate first, including on rejection. Backprop must isolate this
            # user code from committed columns until every row cell succeeds.
            self.params["seen"].append(value)
            if type(value) is not int or value < self.params["minimum"]:
                return types.InvalidType()
            return self

        def to_json(self, artifact=None):
            return {**super().to_json(artifact), "handler_version": 1}

        @classmethod
        def from_json(cls, value, artifact=None):
            if value.get("handler_version") != 1:
                raise ValueError("Unsupported custom dtype version")
            return cls(**value["params"])
    return CountedInteger


@unittest.skipUnless((SOURCE / "_dtypes.py").exists(), "Pinned W&B source snapshot required")
class StructuralDtypeReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference_types = reference_types()
        cls.reference_table = reference_table(cls.reference_types)

    def compare_sequence(self, factory, values, optional=False):
        """Check exact evolving constraints and atomic failure after every row."""
        upstream_dtype, native_dtype = factory(self.reference_types), factory(native)
        reference = self.reference_table(columns=["value"], dtype=upstream_dtype, optional=optional)
        actual = backprop.Table(columns=["value"], dtype=native_dtype, optional=optional)
        original = copy.deepcopy(getattr(native_dtype, "params", {}))
        for value in values:
            with self.subTest(dtype=repr(native_dtype), value=repr(value), optional=optional):
                before = (copy.deepcopy(actual._types[0].to_json()), len(actual.data), actual._generation)
                expected, _ = outcome(lambda: reference.add_data(value))
                observed, _ = outcome(lambda: actual.add_data(value))
                self.assertEqual(observed, expected)
                if observed is not None:
                    self.assertEqual((actual._types[0].to_json(), len(actual.data), actual._generation), before)
                else:
                    self.assertEqual(actual._types[0]._runtime_type.to_json(),
                                     reference._column_types.params["type_map"]["value"].to_json())
                    restored = ColumnType.from_json(json.loads(json.dumps(actual._types[0].to_json(), allow_nan=False)))
                    self.assertEqual(restored._runtime_type.to_json(), actual._types[0]._runtime_type.to_json())
        self.assertEqual(native_dtype.params, original)
        return actual

    def test_unknown_and_union_refinement_matches_pinned(self):
        for optional in (False, True):
            self.compare_sequence(lambda t: t.UnknownType(), [None, 1, 2.5, "bad", 3, float("nan")], optional)
            self.compare_sequence(lambda t: t.UnionType([str, t.UnknownType(), t.UnknownType()]),
                                  ["a", 1, True, {"bad": 1}, None, "b"], optional)
        self.compare_sequence(lambda t: t.UnknownType(), [[], [1, 2], [3], ["bad"], []])

    def test_lists_keep_inference_lengths_and_iterable_acceptance(self):
        cases = [
            (lambda t: t.ListType(), [[], [1, 2], (3,), {4, 5}, ["bad"], [], None]),
            (lambda t: t.ListType(str, 2), [["a", "b"], "cd", {"key": 7}, ["one"], [7], []]),
            (lambda t: t.ListType(t.OptionalType(t.UnknownType())), [[None], [1, None], [2], ["bad"]]),
            (lambda t: t.ListType(t.ListType(int)), [[[1, 2], [3]], [[4]], [["bad"]], []]),
        ]
        for factory, values in cases:
            for optional in (False, True):
                self.compare_sequence(factory, values, optional)

    def test_typed_dict_missing_extra_keys_and_nested_refinement(self):
        factory = lambda t: t.TypedDictType({"values": t.ListType(t.UnknownType()),
                                            "label": t.OptionalType(str)})
        values = [{"values": []}, {"values": [1, 2], "label": "a"},
                  {"values": [3], "extra": 1}, {"values": ["bad"]}, {},
                  {"values": [4], "label": None}, None]
        for optional in (False, True):
            self.compare_sequence(factory, values, optional)
        self.compare_sequence(lambda t: t.TypedDictType(), [{}, {"extra": 1}, {}])
        self.compare_sequence(lambda t: t.UnknownType(), [{"a": 1}, {"a": 2}, {}, {"a": 3, "extra": 1}])

    def test_builtin_declarations_and_mixed_types_match_pinned(self):
        declarations = [list, tuple, set, frozenset, dict, object, any,
                        [[int]], [{"x": int}], [[int, str]], [(1, 2)], [{1, 2}], [frozenset({1, 2})]]
        values = [[], [1, 2], (1, 2), {1, 2}, frozenset({1, 2}), {"x": 1}, {}, "x", 1, None]
        for declaration in declarations:
            for optional in (False, True):
                for value in values:
                    with self.subTest(dtype=repr(declaration), value=repr(value), optional=optional):
                        expected, _ = outcome(lambda: self.reference_table(columns=["v"], data=[[value]], dtype=declaration, optional=optional))
                        actual, _ = outcome(lambda: backprop.Table(columns=["v"], data=[[value]], dtype=declaration, optional=optional))
                        self.assertEqual(actual, expected)
        for optional in (False, True):
            for value in values:
                expected, _ = outcome(lambda: self.reference_table(columns=["v"], data=[[value]], allow_mixed_types=True, optional=optional))
                actual, _ = outcome(lambda: backprop.Table(columns=["v"], data=[[value]], allow_mixed_types=True, optional=optional))
                self.assertEqual(actual, expected)

    def test_added_columns_use_the_same_inference_and_atomic_rejections(self):
        cases = [([1, 2], False), ([None, 2], False), ([None, 2], True),
                 ([[1], [2, None]], False), ([[], [1, 2]], False),
                 ([{"a": 1}, {}], False), ([{"a": None}, {"a": 2}], True),
                 ([{"a": 1}, {"a": 2, "extra": 3}], False)]
        for values, optional in cases:
            with self.subTest(values=values, optional=optional):
                tables = [cls(columns=["base"], data=[[1], [2]]) for cls in (self.reference_table, backprop.Table)]
                before = (list(tables[1].columns), copy.deepcopy(tables[1].data), tables[1]._generation)
                results = [outcome(lambda table=table: table.add_column("added", values, optional)) for table in tables]
                self.assertEqual(results[0][0], results[1][0])
                self.assertEqual(tables[1].data, tables[0].data)
                if results[1][0] is None:
                    self.assertEqual(tables[1]._types[1]._runtime_type.to_json(),
                                     tables[0]._column_types.params["type_map"]["added"].to_json())
                else:
                    self.assertEqual((tables[1].columns, tables[1].data, tables[1]._generation), before)

    def test_constant_containers_match_assignment_and_keep_native_identity(self):
        constants = [(1, "a"), {1, 2}, frozenset({1, 2}), [1, 2], {"a": (1, 2)}]
        values = constants + [(1, "wrong"), set(), {}, None]
        for constant in constants:
            self.compare_sequence(lambda t: t.ConstType(copy.deepcopy(constant)), values)
        self.compare_sequence(lambda t: t.ConstType([1, 2], is_set=True), [{1, 2}, [1, 2], frozenset({1, 2})])

    def test_cast_returns_refined_wbtype_and_failed_cast_is_atomic(self):
        factories = [lambda t: t.ListType(), lambda t: t.UnknownType(),
                     lambda t: t.UnionType([t.ListType(int), str])]
        for factory in factories:
            tables = [self.reference_table(columns=["v"], data=[[[1, 2]], [[3]]]),
                      backprop.Table(columns=["v"], data=[[[1, 2]], [[3]]])]
            results = [table.cast("v", factory(types)) for table, types in zip(tables, (self.reference_types, native))]
            self.assertIsInstance(results[1], native.Type)
            self.assertEqual(results[1].to_json(), results[0].to_json())
            self.assertIs(results[1], tables[1]._types[0]._runtime_type)
            before = (tables[1]._types[0].to_json(), tables[1]._generation)
            with self.assertRaises(TypeError):
                tables[1].cast("v", native.ListType(str))
            self.assertEqual((tables[1]._types[0].to_json(), tables[1]._generation), before)
        # The whole dtype=None inference exception does not apply to cast(None).
        self.assertIsInstance(backprop.Table(columns=["v"], data=[[None]]).cast("v", None), native.ConstType)

    def test_custom_type_calls_match_exact_local_handler_behavior(self):
        # A Type subclass supplies trusted local Python behavior. No source
        # rewriting or approximation of its assign implementation is needed.
        for optional in (False, True):
            for value in (None, 1, 2, 3, True, "3", float("nan")):
                factories = [custom_type(self.reference_types), custom_type(native)]
                classes = [self.reference_table, backprop.Table]
                results = [outcome(lambda cls=cls, factory=factory: cls(columns=["v"], data=[[value]],
                                                                         dtype=factory(2), optional=optional))
                           for cls, factory in zip(classes, factories)]
                self.assertEqual(results[0][0], results[1][0])
                if results[1][0] is None:
                    expected = results[0][1]._column_types.params["type_map"]["v"]
                    actual = results[1][1]._types[0]._runtime_type
                    self.assertEqual(actual.to_json(), expected.to_json())
                    self.assertEqual(actual.assign(4).to_json(), expected.assign(4).to_json())

    def test_ndarray_shapes_and_subclasses_match_pinned(self):
        try:
            np = importlib.import_module("numpy")
        except ImportError:
            self.skipTest("Existing optional NumPy runtime required")
        class ArraySubclass(np.ndarray):
            pass
        for optional in (False, True):
            self.compare_sequence(lambda t: t.NDArrayType([2]),
                                  [[1, 2], np.array([3, 4]), np.array([5, 6]).view(ArraySubclass),
                                   [1], [[1], [2]], (1, 2), None], optional)
        self.compare_sequence(lambda t: t.NDArrayType([2, 2]),
                              [[[1, 2], [3, 4]], [[1, 2], [3]], np.ones((2, 2)), np.ones((2, 3))])
        # Empty ndarray shapes terminate upstream, unlike its empty Python list.
        self.compare_sequence(lambda t: t.NDArrayType([0, 3]), [np.empty((0, 3)), np.empty((0, 2))])

    def test_vendored_type_algorithms_match_source_except_empty_axis_fix(self):
        _, reference = source_tree("_dtypes.py")
        local = ast.parse(Path(native.__file__).read_text())
        # Ignore imports, top-level docstring and type-only Artifact import.
        # Every actual function/class body remains bytecode-equivalent after
        # removing the one deliberate termination branch below.
        upstream_nodes = {node.name: node for node in reference.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
        local_nodes = {node.name: node for node in local.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))}
        ndarray = local_nodes["NDArrayType"]
        from_obj = next(node for node in ndarray.body if isinstance(node, ast.FunctionDef) and node.name == "from_obj")
        loop = next(node for node in ast.walk(from_obj) if isinstance(node, ast.While))
        branch = next(node for node in loop.body if isinstance(node, ast.If))
        self.assertEqual([type(node).__name__ for node in branch.orelse], ["Break"])
        branch.orelse = []
        self.assertEqual(local_nodes.keys(), upstream_nodes.keys())
        for name, node in upstream_nodes.items():
            with self.subTest(definition=name):
                self.assertEqual(ast.dump(local_nodes[name], include_attributes=False), ast.dump(node, include_attributes=False))


class StructuralNativeStorage(unittest.TestCase):
    def test_custom_registration_before_first_table_import_is_valid(self):
        script = """
import sys
sys.path.insert(0, sys.argv[1])
import backprop
from backprop import table_types as types
assert 'backprop.table_dtype_bridge' not in sys.modules
class EarlyCustom(types.Type):
    name = 'early_unique_custom'
    def __init__(self, minimum=2):
        self.params['minimum'] = minimum
    def assign(self, value=None):
        return self if type(value) is int and value >= self.params['minimum'] else types.InvalidType()
types.TypeRegistry.add(EarlyCustom)
table = backprop.Table(columns=['v'], data=[[3]], dtype=EarlyCustom(), optional=False)
schema = table._types[0].to_json()
from backprop.tables import ColumnType
assert isinstance(ColumnType.from_json(schema)._runtime_type, EarlyCustom)
"""
        sdk = str(Path(__file__).resolve().parents[1] / "sdk")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, sdk], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_list_axes_terminate_without_discarding_shape(self):
        for shape, value in (([0], []), ([2, 0], [[], []]), ([1, 1, 0], [[[]]])):
            table = backprop.Table(columns=["v"], dtype=native.NDArrayType(shape), optional=False)
            table.add_data(value)
            self.assertEqual(table._types[0].shape, shape)
        with self.assertRaises(TypeError):
            backprop.Table(columns=["v"], data=[[[]]], dtype=native.NDArrayType([2, 0]), optional=False)

    def test_nullable_schemas_do_not_grow_on_internal_clone_or_artifact_read(self):
        table = backprop.Table(columns=["value"], data=[[1], [None]], dtype=native.UnionType([int, str]))
        schema = table._types[0].to_json()
        for _ in range(3):
            table = backprop.Table(columns=table.columns, data=table.data, dtype=table._types,
                                   optional=[typ.optional for typ in table._types])
            self.assertEqual(table._types[0].to_json(), schema)
        artifact = backprop.Artifact("stable-optional", "dataset")
        artifact.add(table, "table")
        self.assertEqual(artifact.get("table")._types[0].to_json(), schema)
        # Explicitly enabling optionality on a previously nonnullable native
        # wrapper still adds it once and accepts None.
        nonnull = ColumnType.from_dtype(int, False)
        nullable = backprop.Table(columns=["v"], data=[[None]], dtype=[nonnull], optional=True)
        self.assertEqual(nullable.data, [[None]])

    def test_registered_custom_handler_is_atomic_and_reconstructed_explicitly(self):
        handler = custom_type(native)
        original = handler(minimum=2)
        table = backprop.Table(columns=["custom", "constant"], dtype=[original, "ok"], optional=False)
        table.add_data(3, "ok")
        self.assertEqual(original.params["seen"], [])
        before = copy.deepcopy(table._types[0]._runtime_type.params)
        for row in ((4, "bad"), (1, "ok")):
            with self.assertRaises(TypeError):
                table.add_data(*row)
            self.assertEqual(table._types[0]._runtime_type.params, before)
            self.assertEqual(table.data, [[3, "ok"]])
        with self.assertRaisesRegex(TypeError, "registered"):
            table._types[0].to_json()
        native.TypeRegistry.add(handler)
        try:
            schema = json.loads(json.dumps(table._types[0].to_json()))
            self.assertEqual(schema["upstream_schema"]["handler_version"], 1)
            rebuilt = ColumnType.from_json(schema)
            self.assertIsInstance(rebuilt._runtime_type, handler)
            self.assertEqual(rebuilt._runtime_type.params, before)
            self.assertEqual(rebuilt.assign(5)._runtime_type.params["seen"], [3, 5])
            self.assertEqual(rebuilt._runtime_type.params["seen"], [3])
            cast = table.cast("custom", handler(2))
            self.assertIsInstance(cast, handler)
            self.assertEqual(cast.params["seen"], [3])
            schema["upstream_schema"]["handler_version"] = 2
            with self.assertRaises(ValueError):
                ColumnType.from_json(schema)
        finally:
            native.TypeRegistry.types_by_name().pop(handler.name)
        with self.assertRaisesRegex(TypeError, "explicitly registered"):
            ColumnType.from_json({"upstream_schema": {"wb_type": handler.name}})

    def test_custom_handler_artifact_context_reaches_nested_serialization(self):
        context = object()
        calls = []
        class ContextualInteger(native.Type):
            name = "backprop_test_contextual_integer"

            def assign(self, value=None):
                return self if type(value) is int else native.InvalidType()

            def to_json(self, artifact=None):
                if artifact is not context:
                    raise ValueError("Artifact context was lost")
                calls.append("write")
                return {"wb_type": self.name}

            @classmethod
            def from_json(cls, value, artifact=None):
                if artifact is not context:
                    raise ValueError("Artifact context was lost")
                calls.append("read")
                return cls()
        native.TypeRegistry.add(ContextualInteger)
        try:
            dtype = native.TypedDictType({"nested": native.ListType(native.OptionalType(ContextualInteger()))})
            table = backprop.Table(columns=["v"], data=[[{"nested": [1, None]}]], dtype=dtype, optional=False)
            schema = table._types[0].to_json(context)
            restored = ColumnType.from_json(schema, context)
            self.assertEqual(restored.assign({"nested": [2, None]}).to_json(context), schema)
            self.assertIn("write", calls)
            self.assertIn("read", calls)
            linked = backprop.Table(columns=["v"], data=[[1]])
            mixed = ColumnType("dictionary", fields={
                "reference": ColumnType("foreignIndex", False, table=linked),
                "custom": ColumnType.from_dtype(ContextualInteger(), False),
            })
            history_schema = mixed.to_json(context, _relationship_ids=True)
            self.assertEqual(history_schema["fields"]["reference"]["table_id"], linked._id)
            self.assertNotIn("table_path", history_schema["fields"]["reference"])
            self.assertEqual(history_schema["fields"]["custom"]["upstream_schema"]["wb_type"], ContextualInteger.name)
            with self.assertRaisesRegex(ValueError, "context was lost"):
                table._types[0].to_json()
        finally:
            native.TypeRegistry.types_by_name().pop(ContextualInteger.name)

    def test_json_compatible_numpy_constant_scalars_survive_storage(self):
        try:
            np = importlib.import_module("numpy")
        except ImportError:
            self.skipTest("Existing optional NumPy runtime required")
        for value in (np.float64(1.5), np.str_("label")):
            self.assertEqual(json.loads(json.dumps(value)), value)
            column = ColumnType.from_dtype(value, False).assign(value)
            schema = json.loads(json.dumps(column.to_json()))
            restored = ColumnType.from_json(schema)
            restored.assign(value)
            self.assertEqual(restored.constant, value)
            with self.assertRaises(TypeError):
                restored.assign("different")
            artifact = backprop.Artifact("numpy-constant", "dataset")
            artifact.add(backprop.Table(columns=["v"], data=[[value]], dtype=[value], optional=False), "table")
            self.assertEqual(artifact.get("table").data, [[value]])

    def test_custom_numpy_state_is_copied_but_constant_identity_is_retained(self):
        try:
            np = importlib.import_module("numpy")
        except ImportError:
            self.skipTest("Existing optional NumPy runtime required")
        class ArrayCounter(native.Type):
            name = "backprop_test_array_counter"
            def __init__(self):
                self.params["count"] = np.zeros(1, dtype=int)
            def assign(self, value=None):
                self.params["count"][0] += 1
                return self if type(value) is int else native.InvalidType()
        original = ArrayCounter()
        table = backprop.Table(columns=["custom", "literal"], dtype=[original, "ok"], optional=False)
        table.add_data(1, "ok")
        for row in ((2, "bad"), ("bad", "ok")):
            with self.assertRaises(TypeError):
                table.add_data(*row)
            self.assertEqual(table._types[0]._runtime_type.params["count"].tolist(), [1])
            self.assertEqual(original.params["count"].tolist(), [0])
        # Array constants are not JSON-serializable upstream, but their local
        # validation still preserves exact identity even inside OptionalType.
        constant = np.array([1, 2])
        for optional in (False, True):
            local = backprop.Table(columns=["v"], dtype=native.ConstType(constant), optional=optional)
            local.add_data(constant)
            self.assertIs(local.data[0][0], constant)

    def test_custom_schema_requires_unique_name_and_unknown_reader_never_imports(self):
        from unittest.mock import patch
        class CollidingType(native.ConstType):
            name = "const"
        column = ColumnType.from_dtype(CollidingType(3), False)
        with self.assertRaisesRegex(TypeError, "registered"):
            column.to_json()
        native.TypeRegistry.add(CollidingType)
        try:
            with self.assertRaisesRegex(TypeError, "unique name"):
                column.to_json()
        finally:
            native.TypeRegistry.add(native.ConstType)
        with patch("importlib.import_module", side_effect=AssertionError("No logged imports")):
            with self.assertRaisesRegex(TypeError, "explicitly registered"):
                ColumnType.from_json({"upstream_schema": {"wb_type": "untrusted.module.Type"}})

    def test_structures_and_constants_survive_artifact_get_and_further_rows(self):
        handler = custom_type(native)
        native.TypeRegistry.add(handler)
        try:
            row = [{"values": [1, 2], "label": None}, (1, "a"), {2, 3}, frozenset({4, 5}), 3]
            table = backprop.Table(columns=["struct", "tuple", "set", "frozen", "custom"], data=[row], optional=False,
                                   dtype=[native.TypedDictType({"values": native.ListType(int), "label": native.OptionalType(str)}),
                                          native.ConstType(row[1]), native.ConstType(row[2]), native.ConstType(row[3]), handler(2)])
            with tempfile.TemporaryDirectory() as directory:
                sink = backprop.MemorySink()
                artifact = backprop.Artifact("structural-types", "dataset")
                artifact.add(table, "table")
                run = backprop.Run(project="structural-types", dir=directory, mode="offline",
                                   settings={"silent": True, "console": "off", "disable_git": True})
                run.log_artifact(artifact)
                run.finish()
                sync(run.sync_dir, sink=sink)
                retrieved = list(backprop.Api._from_store(sink, project=run.project).run(run.id).logged_artifacts())[0].get("table")
                self.assertEqual(retrieved.data, [row])
                self.assertEqual([type(value) for value in retrieved.data[0]], [dict, tuple, set, frozenset, int])
                self.assertIsInstance(retrieved._types[-1]._runtime_type, handler)
                retrieved.add_data({"values": [3]}, *row[1:-1], 4)
                before = ([typ.to_json() for typ in retrieved._types], len(retrieved.data))
                with self.assertRaises(TypeError):
                    retrieved.add_data({"values": ["bad"]}, *row[1:-1], 5)
                self.assertEqual(([typ.to_json() for typ in retrieved._types], len(retrieved.data)), before)
        finally:
            native.TypeRegistry.types_by_name().pop(handler.name)


if __name__ == "__main__":
    unittest.main()
