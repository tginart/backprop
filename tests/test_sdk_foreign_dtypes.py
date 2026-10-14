"""Already-imported W&B custom Types keep their real engine and serializers.

Architecture: execute the hash-pinned pure official dtype family -> define and
explicitly register custom handlers there -> compare constructor/cast behavior
with the pinned Table -> roundtrip actual Backprop artifacts and queued runs.
Only optional/service imports are replaced by the existing reference fixture.
No handler is imported from a saved schema; unknown/ambiguous registries fail.
Main callers: standard-library and scientific release test discovery.
"""
import copy
import gc
import importlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop import table_types as native
from backprop import foreign_type_family as families
from backprop.tables import ColumnType
from backprop.transport import MemorySink, read_journal, sync
from test_sdk_table_dtype_reference import SOURCE, reference_types, reference_table, outcome


def counted_type(types, name="a_foreign_counted"):
    class Counted(types.Type):
        def __init__(self, minimum=0, seen=None):
            self.params.update(minimum=minimum, seen=list(seen or []))
        def assign(self, value=None):
            self.params["seen"].append(value)
            return self if type(value) is int and value >= self.params["minimum"] else types.InvalidType()
    Counted.name = name
    return Counted


def normalized(value):
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    return value


@unittest.skipUnless((SOURCE / "_dtypes.py").is_file(), "Pinned W&B0.30 source snapshot required")
class ForeignDtypeContracts(unittest.TestCase):
    def setUp(self):
        self.foreign = reference_types()
        self.reference = reference_table(self.foreign)
        self.handler = counted_type(self.foreign)
        self.foreign.TypeRegistry.add(self.handler)
        self.family_index = patch.object(families, "_families", [])
        self.family_index.start()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runs = []
        self.store = MemorySink()

    def tearDown(self):
        for run in self.runs:
            if not run._closed:
                run.finish()
        self.temp.cleanup()
        self.environment.stop()
        self.family_index.stop()

    def new(self, offline=False, resume=None):
        run = backprop.Run(project="foreign-dtypes", entity="tests", id="writer", dir=self.root,
            mode="offline" if offline else "online", resume=resume,
            settings={"console": "off", "silent": True, "disable_git": True, "flush_interval": 3600},
            _sink=None if offline else self.store, _deployment={})
        self.runs.append(run)
        return run

    def test_foreign_class_instance_and_optional_side_effects_match_pinned(self):
        for optional in (False, True):
            for factory in (lambda: self.handler, lambda: self.handler(2)):
                for value in (None, float("nan"), 1, 2, 3, True, "3"):
                    with self.subTest(optional=optional, value=value):
                        expected_error, expected = outcome(lambda: self.reference(columns=["v"], data=[[value]], dtype=factory(), optional=optional))
                        actual_error, actual = outcome(lambda: backprop.Table(columns=["v"], data=[[value]], dtype=factory(), optional=optional))
                        self.assertEqual(actual_error, expected_error)
                        if expected_error is None:
                            result = actual.cast("v", factory(), optional)
                            reference_result = expected.cast("v", factory(), optional)
                            self.assertIs(type(result), type(reference_result))
                            self.assertIsInstance(result, self.foreign.Type)
                            self.assertEqual(normalized(result.to_json()), normalized(reference_result.to_json()))
                            self.assertEqual(normalized(actual._types[0].to_json()["upstream_schema"]), normalized(result.to_json()))
        table = backprop.Table(columns=["v"], data=[[None]], dtype=self.handler(), optional=True)
        child = next(item for item in table._types[0]._runtime_type.params["allowed_types"] if item.name == self.handler.name)
        self.assertEqual(child.params["seen"], [None])

    def test_public_and_explicit_nested_declarations_keep_one_foreign_family(self):
        factories = [lambda: self.foreign.ListType(self.handler(2)),
                     lambda: {"x": self.handler(2)},
                     lambda: self.foreign.TypedDictType({"x": self.foreign.ListType(self.handler(2))}),
                     lambda: [self.foreign.ListType(self.handler(2)), str],
                     lambda: self.foreign.UnionType([self.handler(2), str])]
        values = [[2, 3], {"x": 3}, {"x": [2, 4]}, [2, 3], "valid"]
        for factory, value in zip(factories, values):
            for optional in (False, True):
                with self.subTest(declaration=repr(factory()), optional=optional):
                    # dtype is per-column when the Table receives a list.
                    dtype = factory()
                    actual = backprop.Table(columns=["v"], data=[[value]], dtype=[dtype], optional=optional)
                    expected = self.reference(columns=["v"], data=[[value]], dtype=[factory()], optional=optional)
                    result = actual._types[0]._runtime_type
                    self.assertEqual(result.to_json(), expected._column_types.params["type_map"]["v"].to_json())
                    copied = ColumnType.from_json(json.loads(json.dumps(actual._types[0].to_json())))
                    self.assertIs(type(copied._runtime_type), type(result))
                    self.assertEqual(copied._runtime_type.to_json(), result.to_json())

    def test_custom_subclass_returning_base_type_preserves_cast_return(self):
        foreign = self.foreign
        class CustomList(foreign.ListType):
            name = "a_foreign_custom_list"
        foreign.TypeRegistry.add(CustomList)
        actual = backprop.Table(columns=["v"], data=[[[1, 2]]])
        expected = self.reference(columns=["v"], data=[[[1, 2]]])
        a, b = actual.cast("v", CustomList()), expected.cast("v", CustomList())
        self.assertIs(type(a), foreign.ListType)
        self.assertIs(type(a), type(b))
        self.assertEqual(a.to_json(), b.to_json())
        self.assertEqual(actual._types[0].to_json()["upstream_schema"], a.to_json())
        class CustomNumber(foreign.NumberType):
            name = "a_foreign_custom_number"
        # Before registration, inherited assign_type sees ordinary NumberType
        # for an int and rejects this subclass. Registration also inherits its
        # Python-type mappings, making int infer CustomNumber in that family.
        for cls in (backprop.Table, self.reference):
            with self.assertRaises(TypeError):
                cls(columns=["v"], data=[[1]], dtype=CustomNumber, optional=False)
        foreign.TypeRegistry.add(CustomNumber)
        actual = backprop.Table(columns=["v"], data=[[1]], dtype=CustomNumber, optional=False)
        expected = self.reference(columns=["v"], data=[[1]], dtype=CustomNumber, optional=False)
        self.assertIs(type(actual.cast("v", CustomNumber, False)), CustomNumber)
        self.assertEqual(actual._types[0]._runtime_type.to_json(),
                         expected._column_types.params["type_map"]["v"].to_json())

    def test_rejected_late_cell_does_not_mutate_handler_state_or_input_dtype(self):
        original = self.handler(2)
        table = backprop.Table(columns=["v", "constant"], dtype=[original, "ok"], optional=False)
        table.add_data(3, "ok")
        before = copy.deepcopy(table._types[0]._runtime_type.params)
        with self.assertRaises(TypeError):
            table.add_data(4, "wrong")
        self.assertEqual(table.data, [[3, "ok"]])
        self.assertEqual(table._types[0]._runtime_type.params, before)
        self.assertEqual(original.params, {"minimum": 2, "seen": []})

    def test_missing_changed_and_ambiguous_registrations_fail_without_importing(self):
        table = backprop.Table(columns=["v"], data=[[3]], dtype=self.handler(2), optional=False)
        saved = table._types[0].to_json()
        self.foreign.TypeRegistry.types_by_name().pop(self.handler.name)
        with self.assertRaisesRegex(TypeError, "registered"):
            table._types[0].to_json()
        with patch.object(importlib, "import_module", side_effect=AssertionError("schema import")):
            with self.assertRaisesRegex(TypeError, "registered"):
                ColumnType.from_json(saved)
            with self.assertRaisesRegex(TypeError, "registered"):
                ColumnType.from_json({"upstream_schema": {"wb_type": "untrusted.module.Handler"}})
        replacement = counted_type(self.foreign)
        self.foreign.TypeRegistry.add(replacement)
        with self.assertRaisesRegex(TypeError, "exact handler"):
            table._types[0].to_json()
        self.foreign.TypeRegistry.add(self.handler)
        native_handler = counted_type(native)
        native.TypeRegistry.add(native_handler)
        try:
            with self.assertRaisesRegex(TypeError, "ambiguous"):
                ColumnType.from_json(saved)
        finally:
            native.TypeRegistry.types_by_name().pop(native_handler.name, None)

    def test_literal_wb_type_dictionary_never_selects_or_executes_foreign_handler(self):
        families.family_of(self.handler)
        value = {"wb_type": self.handler.name, "params": {"minimum": 2}}
        table = backprop.Table(columns=["v"], data=[[value]], dtype=native.ConstType(value), optional=False)
        native_handler = counted_type(native)
        native.TypeRegistry.add(native_handler)
        try:
            saved = table._types[0].to_json()
            restored = ColumnType.from_json(saved)
            self.assertIs(type(restored._runtime_type), native.ConstType)
            self.assertEqual(restored.constant, value)
        finally:
            native.TypeRegistry.types_by_name().pop(native_handler.name, None)

    def test_standard_legacy_aliases_match_pinned_and_find_nested_custom_family(self):
        ordinary = [
            {"wb_type": "dictionary", "params": {"type_map": {"x": {"wb_type": "number"}}}},
            {"wb_type": "object", "params": {"class_name": "Example"}},
        ]
        for schema in ordinary:
            with self.subTest(alias=schema["wb_type"]):
                expected = self.foreign.TypeRegistry.type_from_dict(schema)
                actual = ColumnType.from_json({"upstream_schema": schema})._runtime_type
                self.assertEqual(actual.name, expected.name)
                self.assertEqual(actual.to_json(), expected.to_json())
        families.family_of(self.handler)
        nested = {"wb_type": "dictionary", "params": {"type_map": {
            "x": {"wb_type": self.handler.name, "params": {"minimum": 2, "seen": []}},
            "object": {"wb_type": "object", "params": {"class_name": "Example"}},
        }}}
        actual = ColumnType.from_json({"upstream_schema": nested})._runtime_type
        expected = self.foreign.TypeRegistry.type_from_dict(nested)
        self.assertIs(type(actual), type(expected))
        self.assertIs(type(actual.params["type_map"]["x"]), self.handler)
        self.assertEqual(actual.to_json(), expected.to_json())

    def test_resume_owner_selection_ignores_schema_shaped_constant_payloads(self):
        from backprop.implicit_artifacts import restore_table_types
        families.family_of(self.handler)
        value = {"wb_type": self.handler.name, "params": {"minimum": 2}}
        table = backprop.Table(columns=["v"], data=[[value]], dtype=native.ConstType(value), optional=False)
        schemas = [table._types[0].to_json()]
        native_handler = counted_type(native)
        native.TypeRegistry.add(native_handler)
        try:
            # No artifact_path/run exists: this literal must not inspect the
            # coincidentally ambiguous executable handler or request an owner.
            restored = restore_table_types(None, {"column_types": schemas})
            self.assertEqual(restored[0].constant, value)
        finally:
            native.TypeRegistry.types_by_name().pop(native_handler.name, None)

    def test_custom_parameter_payload_does_not_select_a_second_family(self):
        payload = {"wb_type": self.handler.name, "params": {"minimum": 5}}
        class Raw(native.Type):
            name = "a_native_raw_payload"
            def __init__(self, payload=None):
                self.params["payload"] = payload
            def assign(self, value=None):
                return self if value == "ok" else native.InvalidType()
            def to_json(self, artifact=None):
                return {"wb_type": self.name, "raw": self.params["payload"]}
            @classmethod
            def from_json(cls, value, artifact=None):
                return cls(value["raw"])
        native.TypeRegistry.add(Raw)
        families.family_of(self.handler)
        try:
            table = backprop.Table(columns=["v"], data=[["ok"]], dtype=Raw(payload), optional=False)
            restored = ColumnType.from_json(table._types[0].to_json())
            self.assertIs(type(restored._runtime_type), Raw)
            self.assertEqual(restored._runtime_type.params["payload"], payload)
        finally:
            native.TypeRegistry.types_by_name().pop(Raw.name, None)

    def test_fresh_already_loaded_official_module_can_reconstruct_registered_schema(self):
        schema = {"upstream_schema": {"wb_type": self.handler.name, "params": {"minimum": 4, "seen": []}}}
        module = ModuleType(families.MODULE)
        module.__dict__.update(vars(self.foreign))
        self.assertEqual(families._families, [])
        with patch.dict(sys.modules, {families.MODULE: module}), patch.object(importlib, "import_module", side_effect=AssertionError("unexpected import")):
            restored = ColumnType.from_json(schema)
        self.assertIs(type(restored._runtime_type), self.handler)
        with self.assertRaises(TypeError):
            restored.assign(3)
        self.assertEqual(restored.assign(5)._runtime_type.params["seen"], [5])

    def test_family_index_does_not_keep_unloaded_temporary_sdk_code_alive(self):
        def temporary():
            foreign = reference_types()
            family = families.family_of(foreign.Type)
            return family, weakref.ref(foreign.Type), weakref.ref(foreign.TypeRegistry)
        family, base, registry = temporary()
        gc.collect()
        self.assertIsNone(base())
        self.assertIsNone(registry())
        self.assertIsNone(family.handler("number"))

    def test_offline_artifact_and_running_summary_reconstruct_actual_custom_handler(self):
        dtype = self.foreign.TypedDictType({"values": self.foreign.ListType(self.handler(2))})
        table = backprop.Table(columns=["v"], data=[[{"values": [2, 3]}]], dtype=dtype, optional=False)
        run = self.new(offline=True)
        run.log({"table": table})
        run.summary["table"] = table
        run.finish()
        sync(run.sync_dir, sink=self.store)
        api = backprop.Api._from_store(self.store, entity=run.entity, project=run.project)
        artifact = list(api.run(run.id).logged_artifacts())[0]
        restored = artifact.get("table")
        self.assertIsInstance(restored._types[0]._runtime_type, self.foreign.TypedDictType)
        nested = restored._types[0]._runtime_type.params["type_map"]["values"].params["element_type"]
        self.assertIs(type(nested), self.handler)
        restored.add_data({"values": [4]})
        with self.assertRaises(TypeError):
            restored.add_data({"values": [1]})

    def test_nested_native_tables_keep_foreign_columns_and_pinned_assignment(self):
        from test_sdk_native_table_types import source_adapters, params
        from backprop.table_native_types import _TableType
        foreign, reference, adapters = source_adapters()
        custom = counted_type(foreign, "a_nested_foreign_counted")
        foreign.TypeRegistry.add(custom)
        for optional in (False, True):
            actual_inner = backprop.Table(columns=["v", "normal"], data=[[3, "x"]], dtype=[custom(2), str], optional=False)
            expected_inner = reference(columns=["v", "normal"], data=[[3, "x"]], dtype=[custom(2), str], optional=False)
            actual = backprop.Table(columns=["inner", "guard"], data=[[actual_inner, "ok"]], dtype=[backprop.Table, "ok"], optional=optional)
            expected = reference(columns=["inner", "guard"], data=[[expected_inner, "ok"]], dtype=[reference, "ok"], optional=optional)
            result = actual.cast("inner", backprop.Table, optional)
            desired = expected.cast("inner", reference, optional)
            self.assertEqual(params(result), params(desired))
            before = copy.deepcopy(actual._types)
            for other_value, guard in ((3, "ok"), (4, "ok"), (3, "bad")):
                a = backprop.Table(columns=["v", "normal"], data=[[other_value, "x"]], dtype=[custom(2), str], optional=False)
                b = reference(columns=["v", "normal"], data=[[other_value, "x"]], dtype=[custom(2), str], optional=False)
                actual_error, _ = outcome(lambda: actual.add_data(a, guard))
                expected_error, _ = outcome(lambda: expected.add_data(b, guard))
                self.assertEqual(actual_error, expected_error)
                if actual_error:
                    self.assertEqual(actual._types, before)
                else:
                    before = copy.deepcopy(actual._types)
            self.assertEqual(len(actual.data), len(expected.data))
        # Pinned from_json replays row assignment. A codec whose mutable seen
        # list is also its exact assign_type constraint cannot roundtrip as an
        # outer Table constraint in either SDK; use a stateless constrained
        # codec for successful retrieval, retaining Counted's checks above.
        class StableMinimum(custom):
            name = "a_nested_stable_minimum"
            def assign(self, value=None):
                return self if type(value) is int and value >= self.params["minimum"] else foreign.InvalidType()
        foreign.TypeRegistry.add(StableMinimum)
        # Inference and explicit nested containers both keep native TableType,
        # while each inner Table's original custom column handler survives.
        for wrap, dtype in ((lambda value: value, None),
                            (lambda value: [value], native.ListType(_TableType())),
                            (lambda value: {"nested": value}, {"nested": backprop.Table})):
            inner = backprop.Table(columns=["v", "normal"], data=[[3, "x"]], dtype=[StableMinimum(2), str], optional=False)
            outer = backprop.Table(columns=["inner"], data=[[wrap(inner)]], dtype=[dtype] if dtype is not None else None)
            artifact = backprop.Artifact("nested-foreign-" + str(len(self.store.events)), "table")
            artifact.add(outer, "outer")
            run = self.new(resume="allow")
            run.log_artifact(artifact)
            run.finish()
            api = backprop.Api._from_store(self.store, entity=run.entity, project=run.project)
            restored = api.artifact(artifact.qualified_name).get("outer")
            cell = restored.data[0][0]
            recovered = cell[0] if isinstance(cell, list) else cell["nested"] if isinstance(cell, dict) else cell
            self.assertIs(type(recovered._types[0]._runtime_type), StableMinimum)
            self.assertEqual(recovered.data, [[3, "x"]])
            recovered.add_data(4, "y")
            with self.assertRaises(TypeError):
                recovered.add_data(1, "z")

    def test_native_table_schema_preserves_parent_validation(self):
        base = {"wb_type": "table", "params": {"column_types": {
            "wb_type": "typedDict", "params": {"type_map": {"v": {"wb_type": "number"}}}}}}
        for mutate in (lambda value: value.update(untrusted=True),
                       lambda value: value["params"].update(discarded_constraint=True),
                       lambda value: value["params"].update(type_map=[])):
            schema = copy.deepcopy(base)
            mutate(schema["params"]["column_types"])
            with self.assertRaises((TypeError, AttributeError)):
                ColumnType.from_json({"upstream_schema": schema})

    def test_real_artifact_context_and_resume_are_available_to_foreign_codec(self):
        foreign = self.foreign
        owners = []
        class FileMinimum(foreign.Type):
            name = "a_foreign_file_minimum"
            def __init__(self, minimum=2):
                self.params.update(minimum=minimum)
            def assign(self, value=None):
                return self if type(value) is int and value >= self.params["minimum"] else foreign.InvalidType()
            def to_json(self, artifact=None):
                if not isinstance(artifact, backprop.Artifact):
                    raise ValueError("A real artifact owner is required")
                owners.append(artifact)
                name = "constraint.txt"
                if name not in artifact._entries:
                    with artifact.new_file(name) as output:
                        output.write(str(self.params["minimum"]))
                return {"wb_type": self.name, "constraint": name}
            @classmethod
            def from_json(cls, value, artifact=None):
                if not isinstance(artifact, backprop.Artifact):
                    raise ValueError("A real artifact owner is required")
                owners.append(artifact)
                return cls(int(artifact.get_entry(value["constraint"])._read_verified_bytes()))
        foreign.TypeRegistry.add(FileMinimum)
        run = self.new()
        table = backprop.Table(columns=["v"], data=[[{"items": [3]}]], dtype=foreign.TypedDictType({"items": foreign.ListType(FileMinimum())}), optional=False, log_mode="INCREMENTAL")
        run.log({"table": table})
        run.finish()
        self.assertTrue(owners)
        resumed = self.new(resume="must")
        extension = backprop.Table(columns=["v"], data=[[{"items": [4]}]], dtype=foreign.TypedDictType({"items": foreign.ListType(FileMinimum())}), optional=False, log_mode="INCREMENTAL")
        resumed.log({"table": extension})
        resumed.finish()
        rows = [event for event in read_journal(resumed.sync_dir) if event["type"] == "history"]
        self.assertEqual([event["payload"]["metrics"]["table"]["row_offset"] for event in rows], [0, 1])
        self.assertTrue(all(isinstance(owner, backprop.Artifact) for owner in owners))


if __name__ == "__main__":
    unittest.main()
