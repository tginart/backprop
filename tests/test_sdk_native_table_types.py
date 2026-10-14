"""Exact native WBType adapter contracts against pinned source classes.

Architecture: execute hash-pinned pure reference classes with the reference
TypeRegistry -> compare params, assignment, casts and value/example resolution
-> verify immutable native storage and history relationship representations.
Reference fixtures replace only media/service construction, not type algorithms.
Main callers: unittest discovery; optional Pillow cases are explicitly marked.
"""
import ast
import base64
import copy
import hashlib
import json
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import backprop as w
from backprop import table_types as native
from backprop import table_native_types as adapters
from backprop.table_image_type import _ImageFileType
from backprop.tables import ColumnType
from backprop.transport import sync
from test_sdk_table_dtype_reference import SOURCE, reference_types, reference_table, outcome


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z4aMAAAAASUVORK5CYII=")
HASHES = {
    "table.py": "652cc8b4a9d51ef78917a5ea213ac5ccb9af4fce704a2293c812a01d9aaa96bd",
    "image.py": "ebb428a441432f964264e3c593ef99b8de1a006c92a4486dcdee002aa2711373",
    "audio.py": "9cd5ec160d3799c57934024838a4da3abafda117daa910f5720fe2888411b640",
    "video.py": "364916680428208f4299d21f096ade0c175e2f70bee8b26c8899818310491dc4",
    "html.py": "7b628bbb80e96327e77df3c837bc663e2cdd94939e84df2943390d438bdb5e7c",
    "helper_types/classes.py": "6db152b6d299b49079a8d571f517aecedbdfa833b8aa475fd6c6a2cc4ff70364",
}


def execute_classes(file, names, namespace):
    """Compile complete source class bodies without importing remote services."""
    path = SOURCE / file
    if hashlib.sha256(path.read_bytes()).hexdigest() != HASHES[file]:
        raise AssertionError(f"Unexpected pinned source hash: {file}")
    namespace["__name__"] = "wandb.sdk.data_types." + file.removesuffix(".py").replace("/", ".")
    tree = ast.parse(path.read_text())
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in names]
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *classes], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)


def source_adapters():
    types = reference_types()
    table = reference_table(types)
    ns = table.__init__.__globals__
    ns.update(Table=table, Classes=w.Classes, Image=w.Image, Audio=w.Audio, Video=w.Video, Html=w.Html,
              runid=SimpleNamespace(generate_id=lambda: "reference"), os=__import__("os"))
    execute_classes("table.py", {"_TableLinkMixin", "_TableKey", "_TableIndex", "_PrimaryKeyType", "_ForeignKeyType", "_ForeignIndexType", "_TableType"}, ns)
    # Restore real key updates, public setters and comparison omitted by the
    # earlier scalar-only fixture. Their class globals are now the real adapters.
    source = ast.parse((SOURCE / "table.py").read_text())
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "Table")
    names = {"_update_keys", "_apply_key_updates", "set_pk", "set_fk", "_eq_debug", "__eq__", "__ne__", "get_index", "index_ref"}
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for method in methods:
        method.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *methods], type_ignores=[]))
    exec(compile(module, str(SOURCE / "table.py"), "exec"), ns)
    for name in names:
        setattr(table, name, ns[name])
    for path, name in (("helper_types/classes.py", "_ClassesIdType"), ("image.py", "_ImageFileType"),
                       ("audio.py", "_AudioFileType"), ("video.py", "_VideoFileType"), ("html.py", "_HtmlFileType")):
        execute_classes(path, {name}, ns)
    for name in ("_PrimaryKeyType", "_ForeignKeyType", "_ForeignIndexType", "_TableType", "_ClassesIdType",
                 "_ImageFileType", "_AudioFileType", "_VideoFileType", "_HtmlFileType"):
        types.TypeRegistry.add(ns[name])
    return types, table, SimpleNamespace(**ns)


def params(value):
    """Compare semantics across separate reference/native Type identities."""
    if hasattr(value, "name") and hasattr(value, "params"):
        return {"name": value.name, "params": params(value.params)}
    if isinstance(value, dict):
        return {key: params(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [params(item) for item in value]
    return value


@unittest.skipUnless((SOURCE / "image.py").exists(), "Pinned W&B source snapshot required")
class NativeTypeReference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.types, cls.Table, cls.source = source_adapters()

    def test_simple_media_classes_and_examples_follow_registry(self):
        for media, type_name in ((w.Audio, "audio-file"), (w.Video, "video-file"), (w.Html, "html-file"), (w.Image, "image-file")):
            self.assertEqual(native.TypeRegistry.type_from_dtype(media).name, type_name)
            self.assertEqual(self.types.TypeRegistry.type_from_dtype(media).name, type_name)
            example = object.__new__(media)
            self.assertEqual(native.TypeRegistry.type_from_dtype(example).name, "const")
            self.assertEqual(self.types.TypeRegistry.type_from_dtype(example).name, "const")
        # No upstream registration exists for Plotly or Histogram classes.
        for media in (w.Plotly, w.Histogram):
            self.assertEqual(params(native.TypeRegistry.type_from_dtype(media)), params(self.types.TypeRegistry.type_from_dtype(media)))

    def test_classes_id_params_assign_type_and_cast_returns(self):
        classes = w.Classes([{"id": 1, "name": "one"}, {"id": "two", "name": "two"}])
        actual, expected = classes.get_type(), self.source._ClassesIdType(classes)
        self.assertIsInstance(actual, native.Type)
        self.assertEqual(params(actual), params(expected))
        for value in (1, 1.0, True, "two", 2, None):
            self.assertEqual(params(actual.assign(value)), params(expected.assign(value)))
        for factory in (lambda t: t.ConstType(1), lambda t: t.NumberType(), lambda t: t.StringType(), lambda t: t.ConstType("two")):
            self.assertEqual(params(actual.assign_type(factory(native))), params(expected.assign_type(factory(self.types))))
        tables = [cls(columns=["v"], data=[[1], ["two"]], allow_mixed_types=True) for cls in (self.Table, w.Table)]
        result_expected = tables[0].cast("v", expected)
        result_actual = tables[1].cast("v", actual)
        self.assertIsInstance(result_actual, adapters._ClassesIdType)
        self.assertEqual(params(result_actual), params(result_expected))
        for cls in (self.Table, w.Table):
            with self.assertRaises(TypeError):
                cls(columns=["v"], data=[[1]], dtype=classes)
        literal = w.Table(columns=["v"], data=[[classes]], dtype=classes, optional=False)
        self.assertEqual(literal._types[0]._runtime_type.name, "const")
        with self.assertRaises(TypeError):
            w.Artifact("literal-classes", "dataset").add(literal, "table")

    def test_image_class_schema_merges_layers_scores_and_existing_names(self):
        kwargs = [
            {"box_layers": {"pred": {1}}, "box_score_keys": ["p"], "box_class_maps": {"pred": {1: "first"}}, "class_map": {"1": "first"}},
            {"box_layers": {"pred": {1, 2}, "truth": {3}}, "box_score_keys": ["q"], "mask_layers": {"seg": {4}},
             "box_class_maps": {"pred": {1: "replacement", 2: "second"}}, "mask_class_maps": {"seg": {4: "mask"}}, "class_map": {"1": "replacement", "2": "second"}},
        ]
        actual, expected = _ImageFileType(), self.source._ImageFileType()
        for options in kwargs:
            old = params(actual)
            actual_next = actual.assign_type(_ImageFileType(**options))
            expected = expected.assign_type(self.source._ImageFileType(**options))
            self.assertEqual(params(actual), old)
            actual = actual_next
            self.assertEqual(params(actual), params(expected))
        self.assertEqual(actual.params["box_class_maps"].params["val"]["pred"]["1"], "first")
        self.assertEqual(actual.assign_type(native.StringType()).name, "invalid")
        for key, bad in (("box_layers", []), ("box_score_keys", "score"), ("mask_layers", []), ("class_map", [])):
            if bad == []:  # Falsy inputs normalize to an empty default upstream.
                continue
            self.assertEqual(outcome(lambda: _ImageFileType(**{key: bad}))[0], outcome(lambda: self.source._ImageFileType(**{key: bad}))[0])

    def test_image_inference_reads_real_native_annotations(self):
        box = {"position": {"minX": 0, "minY": 0, "maxX": 1, "maxY": 1}, "class_id": 1, "scores": {"p": 0.5}}
        image = w.Image(PNG, boxes={"pred": {"box_data": [box], "class_labels": {1: "one", 2: "two"}}},
                        classes=w.Classes([{"id": 3, "name": "three"}]))
        reference = object.__new__(w.Image)
        reference._boxes = {"pred": SimpleNamespace(_val=[box], _class_labels={1: "one", 2: "two"})}
        reference._masks = {}
        reference._classes = w.Classes([{"id": 1, "name": "one"}, {"id": 2, "name": "two"}, {"id": 3, "name": "three"}])
        self.assertEqual(params(_ImageFileType.from_obj(image)), params(self.source._ImageFileType.from_obj(reference)))
        table = w.Table(columns=["image"], data=[[image]], dtype=w.Image)
        self.assertIsInstance(table.cast("image", w.Image), _ImageFileType)
        before = table._types[0].to_json()
        with self.assertRaises(TypeError):
            table.add_data("wrong")
        self.assertEqual(table._types[0].to_json(), before)

    def test_constructor_forms_and_nested_optional_constraints_match_source(self):
        for ids in (None, [], [1, "two"], 1, {1, 2}, "wrong"):
            expected = outcome(lambda: self.source._ClassesIdType(valid_ids=ids))
            actual = outcome(lambda: adapters._ClassesIdType(valid_ids=ids))
            self.assertEqual(actual[0], expected[0])
            if actual[0] is None:
                self.assertEqual(params(actual[1]), params(expected[1]))
        for classes, types, module in ((self.Table, self.types, self.source), (w.Table, native, adapters)):
            target = classes(columns=["v"], data=[[1]])
            nested = types.TypedDictType({"refs": types.ListType(module._ForeignIndexType(target))})
            table = classes(columns=["nested"], data=[[None], [{"refs": [target.index_ref(0)]}]], dtype=nested)
            self.assertEqual(table.data[0], [None])
            result = table.cast("nested", nested, optional=True)
            self.assertEqual(result.name, "union")
            self.assertEqual(result.assign({"refs": [0]}).name, "union")
            self.assertEqual(result.assign({"refs": ["bad"]}).name, "invalid")
            other = classes(columns=["v"], data=[[1]])
            self.assertEqual(result.assign({"refs": [other.index_ref(0)]}).name, "invalid")

    def test_image_refinement_and_late_row_failure_preserve_prior_constraints(self):
        def image(layer, class_id, score):
            box = {"position": {"minX": 0, "minY": 0, "maxX": 1, "maxY": 1}, "class_id": class_id, "scores": {score: 0.5}}
            return w.Image(PNG, boxes={layer: {"box_data": [box], "class_labels": {class_id: str(class_id)}}})
        original, candidate = image("first", 1, "p"), image("new", 2, "q")
        table = w.Table(columns=["image", "label"], data=[[original, "valid"]], dtype=[w.Image, str], optional=False)
        before = copy.deepcopy(table._types[0].to_json())
        generation = table._generation
        with self.assertRaises(TypeError):
            table.add_data(candidate, 123)
        self.assertEqual(table._types[0].to_json(), before)
        self.assertEqual(len(table.data), 1)
        self.assertEqual(table._generation, generation)
        table.add_data(candidate, "valid")
        result = table.cast("image", w.Image)
        self.assertEqual(result.params["box_layers"].params["val"], {"first": {1}, "new": {2}})
        self.assertEqual(result.params["box_score_keys"].params["val"], {"p", "q"})

    @unittest.skipUnless(importlib.util.find_spec("PIL") and importlib.util.find_spec("numpy"), "Existing optional Pillow/NumPy runtime required")
    def test_mask_and_fractional_annotation_ids_match_source_inference(self):
        import numpy as np
        box = {"position": {"minX": 0, "minY": 0, "maxX": 1, "maxY": 1}, "class_id": 0}
        image = w.Image(PNG, boxes={"pred": {"box_data": [box], "class_labels": {1.5: "half"}}},
                        masks={"seg": {"mask_data": np.array([[0, 1]], dtype=np.uint8), "class_labels": {0: "none", 1: "one"}}})
        reference = object.__new__(w.Image)
        reference._boxes = {"pred": SimpleNamespace(_val=[box], _class_labels={1.5: "half"})}
        reference._masks = {"seg": SimpleNamespace(_val={"class_labels": {0: "none", 1: "one"}})}
        reference._classes = image.metadata["classes"]
        self.assertEqual(params(_ImageFileType.from_obj(image)), params(self.source._ImageFileType.from_obj(reference)))

    def test_relationship_assign_type_uses_table_identity(self):
        actual_tables = [w.Table(columns=["key"], data=[["a"]]) for _ in range(2)]
        expected_tables = [self.Table(columns=["key"], data=[["a"]]) for _ in range(2)]
        for name, make in (("_ForeignKeyType", lambda cls, table: cls(table, "key")), ("_ForeignIndexType", lambda cls, table: cls(table))):
            native_cls, reference_cls = getattr(adapters, name), getattr(self.source, name)
            actual, expected = make(native_cls, actual_tables[0]), make(reference_cls, expected_tables[0])
            for index in (0, 1):
                self.assertEqual(actual.assign_type(make(native_cls, actual_tables[index])).name,
                                 expected.assign_type(make(reference_cls, expected_tables[index])).name)
            for scalar in ("StringType", "NumberType", "BooleanType", "NoneType"):
                self.assertEqual(actual.assign_type(getattr(native, scalar)()).name,
                                 expected.assign_type(getattr(self.types, scalar)()).name)
            with self.assertRaises(AssertionError):
                actual.to_json()
        actual = adapters._PrimaryKeyType()
        expected = self.source._PrimaryKeyType()
        for value in ("a", 1, None, True):
            self.assertEqual(actual.assign(value).name, expected.assign(value).name)

    def test_nullable_relationship_unions_keep_scalars_and_none_like_pinned(self):
        for name, rows in (("_PrimaryKeyType", [["a"], [None]]),
                           ("_ForeignKeyType", [["a"], [None]]),
                           ("_ForeignIndexType", [[0], [None]])):
            for table_cls, module in ((self.Table, self.source), (w.Table, adapters)):
                target = table_cls(columns=["key"], data=[["a"]])
                handler = getattr(module, name)
                dtype = handler() if name == "_PrimaryKeyType" else handler(target, "key") if name == "_ForeignKeyType" else handler(target)
                table = table_cls(columns=["v"], dtype=dtype, data=rows)
                self.assertEqual(table.data, rows)
                self.assertEqual(type(table.data[0][0]), type(rows[0][0]))
                self.assertEqual(table.cast("v", dtype, optional=True).name, "union")
                self.assertEqual(table._column_types.params["type_map"]["v"].assign(None).name, "union")
                invalid = "wrong" if name == "_ForeignIndexType" else 1
                with self.assertRaises(TypeError):
                    table.add_data(invalid)
                self.assertEqual(len(table.data), 2)

    def test_empty_declared_keys_initialize_roles_and_reject_removal_like_pinned(self):
        for table_cls, module in ((self.Table, self.source), (w.Table, adapters)):
            table = table_cls(columns=["key"], dtype=module._PrimaryKeyType(), optional=False)
            self.assertEqual(table._pk_col, "key")
            with self.assertRaises(AssertionError):
                table.cast("key", module._PrimaryKeyType())
            with self.assertRaises(AssertionError):
                table_cls(columns=["a", "b"], dtype=module._PrimaryKeyType(), optional=False)
            target = table_cls(columns=["key"], data=[["a"]])
            target.set_pk("key")
            with self.assertRaises(AssertionError):
                table_cls(columns=["key"], dtype=module._PrimaryKeyType(), optional=False, data=[[target.data[0][0]]])
            referenced = table_cls(columns=["i"], dtype=module._ForeignIndexType(target), optional=False)
            self.assertEqual(referenced._fk_cols, {"i"})
            with self.assertRaises(AssertionError):
                referenced.cast("i", module._ForeignIndexType(target), optional=True)

    def test_table_class_refines_nested_column_types_and_rejects_mismatch(self):
        actual, expected = adapters._TableType(), self.source._TableType()
        for rows in ([[1], [2]], [[3]], [["bad"]]):
            observed = actual.assign(w.Table(columns=["v"], data=rows))
            wanted = expected.assign(self.Table(columns=["v"], data=rows))
            self.assertEqual(params(observed), params(wanted))
            if observed.name != "invalid":
                actual, expected = observed, wanted
        inner = w.Table(columns=["v"], data=[[1]])
        outer = w.Table(columns=["table"], data=[[inner]], dtype=w.Table)
        self.assertIsInstance(outer.cast("table", w.Table), adapters._TableType)

    def test_public_reference_codecs_roundtrip_with_exact_table_identity(self):
        class Owner:
            def __init__(self, target):
                self.target = target
            def add(self, value, name):
                self.added = value
                return SimpleNamespace(path="referenced.table.json", digest="digest")
            def get(self, name):
                return self.target if name == "referenced.table.json" else None
        actual_table, expected_table = w.Table(columns=["k"], data=[["a"]]), self.Table(columns=["k"], data=[["a"]])
        for name in ("_ForeignKeyType", "_ForeignIndexType"):
            results = []
            for module, table in ((adapters, actual_table), (self.source, expected_table)):
                handler, owner = getattr(module, name), Owner(table)
                value = handler(table, "k") if name == "_ForeignKeyType" else handler(table)
                encoded = value.to_json(owner)
                self.assertIs(owner.added, table)
                rebuilt = handler.from_json(encoded, owner)
                self.assertIs(rebuilt.params["table"], table)
                with self.assertRaises(AssertionError):
                    handler.from_json(encoded, None)
                results.append(encoded)
            self.assertEqual(results[0], results[1])
        classes = w.Classes([{"id": 1, "name": "one"}])
        self.assertEqual(classes.get_type().to_json(None), self.source._ClassesIdType(classes).to_json(None))
        self.assertEqual(params(adapters._ClassesIdType.from_json(classes.get_type().to_json(None))), params(classes.get_type()))

    def test_adapter_algorithms_match_complete_pinned_class_bodies(self):
        native_modules = {
            "table.py": (adapters, ["_PrimaryKeyType", "_ForeignKeyType", "_ForeignIndexType", "_TableType"]),
            "helper_types/classes.py": (adapters, ["_ClassesIdType"]),
            "audio.py": (adapters, ["_AudioFileType"]),
            "video.py": (adapters, ["_VideoFileType"]),
            "html.py": (adapters, ["_HtmlFileType"]),
        }
        from backprop import table_image_type
        native_modules["image.py"] = (table_image_type, ["_ImageFileType"])
        for filename, (module, names) in native_modules.items():
            reference = {node.name: node for node in ast.parse((SOURCE / filename).read_text()).body if isinstance(node, ast.ClassDef)}
            local = {node.name: node for node in ast.parse(Path(module.__file__).read_text()).body if isinstance(node, ast.ClassDef)}
            if filename == "image.py":
                method = next(node for node in local["_ImageFileType"].body if isinstance(node, ast.FunctionDef) and node.name == "from_obj")
                branch = next(node for node in method.body if isinstance(node, ast.If))
                bridge = branch.orelse.pop(0)
                self.assertEqual(ast.unparse(bridge), "py_obj = _image_state(py_obj)")
            for name in names:
                with self.subTest(adapter=name):
                    self.assertEqual(ast.dump(local[name], include_attributes=False), ast.dump(reference[name], include_attributes=False))

    def test_import_aliases_share_registered_type_identities(self):
        import importlib
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        table_module = importlib.import_module("wandb.sdk.data_types.table")
        image_module = importlib.import_module("wandb.sdk.data_types.image")
        classes_module = importlib.import_module("wandb.sdk.data_types.helper_types.classes")
        for name in ("_PrimaryKeyType", "_ForeignKeyType", "_ForeignIndexType", "_TableType"):
            self.assertIs(getattr(table_module, name), getattr(adapters, name))
        self.assertIs(image_module._ImageFileType, _ImageFileType)
        self.assertIs(classes_module._ClassesIdType, adapters._ClassesIdType)
        self.assertIsInstance(w.Table(columns=["v"], data=[[1]]).index_ref(0), table_module._TableLinkMixin)

    def test_literal_tables_compare_schema_and_data_like_pinned(self):
        for cls, types in ((self.Table, self.types), (w.Table, native)):
            first = cls(columns=["v"], data=[[1]], dtype=int)
            same = cls(columns=["v"], data=[[1]], dtype=int)
            different_type = cls(columns=["v"], data=[[1]], dtype=types.AnyType())
            self.assertEqual(first, same)
            self.assertNotEqual(first, different_type)
            literal = cls(columns=["nested"], data=[[same]], dtype=first)
            self.assertEqual(literal.data, [[same]])
            with self.assertRaises(TypeError):
                literal.add_data(different_type)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Existing optional Pillow runtime required")
    def test_literal_images_compare_pixels_caption_grouping_and_classes(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGB", (2, 1), (10, 20, 30)).save(stream, format="PNG")
        raw = stream.getvalue()
        a, b = w.Image(raw), w.Image(raw)
        c = w.Image(raw, caption="different")
        # Execute the exact source comparison with its expected attribute view.
        tree = ast.parse((SOURCE / "image.py").read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Image")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__eq__")
        module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
        namespace = {"Image": w.Image}
        exec(compile(module, str(SOURCE / "image.py"), "exec"), namespace)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        image_path = Path(directory.name) / "local.png"
        image_path.write_bytes(raw)
        for value in (a, b, c):
            value._path, value._caption, value._grouping = str(image_path), value.caption, value.metadata.get("grouping")
            value._width, value._height = value.image.size
            value._classes = value.metadata.get("classes")
        for other in (a, b, c, object()):
            self.assertEqual(a == other, namespace["__eq__"](a, other))
        table = w.Table(columns=["image"], data=[[b]], dtype=a, optional=False)
        self.assertEqual(table._types[0]._runtime_type.name, "const")
        with self.assertRaises(TypeError):
            table.add_data(c)
        with self.assertRaises(TypeError):
            w.Artifact("literal-image", "dataset").add(table, "table")


class NativeTypeStorage(unittest.TestCase):
    def test_nullable_reference_union_roundtrip_preserves_target_and_none(self):
        target = w.Table(columns=["v"], data=[[1]])
        table = w.Table(columns=["i"], dtype=adapters._ForeignIndexType(target), data=[[0], [None]])
        artifact = w.Artifact("nullable-reference", "dataset")
        artifact.add(table, "table")
        restored = artifact.get("table")
        self.assertEqual(restored.data, [[0], [None]])
        self.assertIs(type(restored.data[0][0]), int)
        children = restored._types[0]._runtime_type.params["allowed_types"]
        reference = next(child for child in children if child.name == "foreignIndex")
        self.assertIsInstance(reference.params["table"], w.Table)
        restored.add_data(None)
        # An actual index object requests promotion to a nonnullable direct
        # relationship, which cannot reinterpret existing None rows upstream.
        with self.assertRaises(TypeError):
            restored.add_data(reference.params["table"].index_ref(0))
        self.assertEqual(restored.data, [[0], [None], [None]])

    def test_nested_relationships_keep_targets_constraints_and_history_navigation(self):
        target = w.Table(columns=["key"], data=[["a"]])
        target.set_pk("key")
        schema = native.TypedDictType({"keys": native.ListType(adapters._ForeignKeyType(target, "key")),
                                       "index": adapters._ForeignIndexType(target)})
        table = w.Table(columns=["nested"], data=[[{"keys": [target.data[0][0]], "index": target.index_ref(0)}]], dtype=schema)
        with tempfile.TemporaryDirectory() as directory:
            run = w.Run(project="nested-types", mode="offline", dir=directory,
                        settings={"silent": True, "console": "off", "disable_git": True})
            run.log({"table": table})
            run.finish()
            sink = w.MemorySink()
            sync(run.sync_dir, sink=sink)
            reader = w.Api._from_store(sink, project=run.project).run(run.id)
            history = list(reader.scan_history())[0]["table"]
            self.assertEqual(set(history["related_tables"]), {target._id})
            fields = history["column_types"][0]["fields"]
            self.assertEqual(fields["keys"]["item_type"]["table_id"], target._id)
            self.assertEqual(fields["index"]["table_id"], target._id)
            restored = list(reader.logged_artifacts())[0].get("table")
            raw = json.loads(list(reader.logged_artifacts())[0].get_entry("table.table.json")._read_verified_bytes())
            stored_fields = raw["column_types"][0]["fields"]
            self.assertEqual(stored_fields["keys"]["item_type"]["table_path"], stored_fields["index"]["table_path"])
            self.assertNotIn("table_path", fields["keys"]["item_type"])
            runtime = restored._types[0]._runtime_type
            constraints = next(child for child in runtime.params["allowed_types"] if child.name == "typedDict").params["type_map"]
            first_target = constraints["keys"].params["element_type"].params["table"]
            self.assertIs(first_target, constraints["index"].params["table"])
            restored.add_data({"keys": ["a"], "index": 0})
            before = len(restored.data)
            with self.assertRaises(TypeError):
                restored.add_data({"keys": ["a"], "index": "bad"})
            self.assertEqual(len(restored.data), before)
            other = w.Table(columns=["key"], data=[["a"]])
            with self.assertRaises(TypeError):
                restored.add_data({"keys": ["a"], "index": other.index_ref(0)})

    def test_nested_table_type_and_image_schema_survive_immutable_roundtrip(self):
        image = w.Image(PNG, boxes={"pred": {"box_data": [{"position": {"minX": 0, "minY": 0, "maxX": 1, "maxY": 1}, "class_id": 1}], "class_labels": {1: "one"}}})
        inner = w.Table(columns=["image"], data=[[image]], dtype=w.Image)
        outer = w.Table(columns=["tables"], data=[[[inner]]], dtype=native.ListType(adapters._TableType()), optional=False)
        artifact = w.Artifact("nested-media-type", "dataset")
        artifact.add(outer, "table")
        restored = artifact.get("table")
        returned = restored.cast("tables", native.ListType(adapters._TableType()))
        self.assertIsInstance(returned.params["element_type"], adapters._TableType)
        image_type = returned.params["element_type"].params["column_types"].params["type_map"]["image"]
        image_type = next(value for value in image_type.params["allowed_types"] if value.name != "none")
        self.assertIsInstance(image_type, _ImageFileType)
        self.assertEqual(image_type.params["box_layers"].params["val"], {"pred": {1}})

    def test_nested_relationship_cycle_rejects_before_journal_or_cursor_changes(self):
        # Empty arrays make valid circular type declarations without cyclic
        # Python data. Native snapshot traversal must still detect the cycle.
        left, right = w.Table(columns=["refs"], data=[[[]]]), w.Table(columns=["refs"], data=[[[]]])
        left.cast("refs", native.ListType(adapters._ForeignIndexType(right)))
        right.cast("refs", native.ListType(adapters._ForeignIndexType(left)))
        with tempfile.TemporaryDirectory() as directory:
            run = w.Run(project="cycle-types", mode="offline", dir=directory,
                        settings={"silent": True, "console": "off", "disable_git": True})
            before = (run.step, left._offset, right._offset)
            with self.assertRaisesRegex(ValueError, "cycle"):
                run.log({"table": left})
            self.assertEqual((run.step, left._offset, right._offset), before)
            self.assertIsNone(left._snapshot)
            self.assertIsNone(right._snapshot)
            run.log({"valid": 1})
            run.finish()
            sink = w.MemorySink()
            sync(run.sync_dir, sink=sink)
            reader = w.Api._from_store(sink, project=run.project).run(run.id)
            self.assertEqual(len(list(reader.scan_history())), 1)
            self.assertEqual(list(reader.logged_artifacts()), [])

    def test_history_and_artifact_keep_relationships_and_native_wbtypes(self):
        parent = w.Table(columns=["key"], data=[["a"]])
        parent.set_pk("key")
        classes = w.Classes([{"id": 1, "name": "one"}])
        table = w.Table(columns=["key", "index", "class"], data=[[parent.data[0][0], parent.index_ref(0), 1]],
                        dtype=[adapters._ForeignKeyType(parent, "key"), adapters._ForeignIndexType(parent), classes.get_type()], optional=False)
        with tempfile.TemporaryDirectory() as directory:
            sink = w.MemorySink()
            run = w.Run(project="native-types", dir=directory, mode="offline", settings={"silent": True, "console": "off", "disable_git": True})
            run.log({"table": table})
            run.finish()
            sync(run.sync_dir, sink=sink)
            reader = w.Api._from_store(sink, project=run.project).run(run.id)
            history = list(reader.scan_history())[0]["table"]
            self.assertEqual(history["column_types"][0]["table_id"], parent._id)
            self.assertNotIn("table_path", history["column_types"][0])
            artifact = list(reader.logged_artifacts())[0]
            restored = artifact.get("table")
            self.assertIsInstance(restored._types[0]._runtime_type, adapters._ForeignKeyType)
            self.assertIsInstance(restored._types[2]._runtime_type, adapters._ClassesIdType)
            self.assertIs(restored.data[0][0]._table, restored.data[0][1]._table)
            restored.add_data("a", 0, 1)
            with self.assertRaises(TypeError):
                restored.add_data("a", 0, 2)


if __name__ == "__main__":
    unittest.main()
