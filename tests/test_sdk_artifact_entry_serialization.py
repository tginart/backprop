"""Manifest export contracts against the pinned W&B model, without its services.

Architecture: hash-checked reference classes -> identical constructed entries ->
Python/JSON outputs, filtering and error comparisons. Separate subprocesses also
exercise the public import alias. Framework dependencies and network access are
unnecessary. Native persistence fields must never enter public serialization.
"""
from __future__ import annotations

from datetime import date
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import warnings

from backprop.artifacts import ArtifactManifestEntry
from test_sdk_artifact_entry import SOURCE_ROOT, reference_entry


HASHES = {
    '_pydantic/base.py': '978799eac3212de7e13d354d28f603e7e41fb123154fb1b1132b35660d76cae2',
    'sdk/lib/paths.py': 'dccb13703649d2d5c739da3f67bac48cf3d37a57fb9c8d35c8023f6c26b233dc',
    '_strutils.py': '86979c08233ad728e26014c7f4bac6eb17e4b9dde602f1934113cdb8806e48c7',
    'sdk/artifacts/_models/base_model.py': '24db3d0229f5fa25fb6470e461f1053b40d464910efabc8da2770ae630a000c3',
    'sdk/artifacts/artifact_manifest_entry.py': '2867734d35b5db75a74b2e3ab472f9baaaf033366b52ec6e10ad6483f985b712',
}


def checked_reference():
    """Reject changed source before executing the original model definitions.

    Main callers: this suite and its isolated subprocess probe. Hash checking
    prevents source drift from turning a comparison into a different reference.
    """
    for name, digest in HASHES.items():
        if hashlib.sha256((SOURCE_ROOT / name).read_bytes()).hexdigest() != digest:
            raise AssertionError(f'Pinned W&B 0.30.0 source changed: {name}')
    return reference_entry().ArtifactManifestEntry


@unittest.skipUnless(importlib.util.find_spec('pydantic') and SOURCE_ROOT.exists(),
                     'Installed Pydantic and pinned W&B source are required')
class EntrySerializationContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Reference = checked_reference()

    def pair(self, **changes):
        args = {'path': 'file', 'digest': 'abc', **changes}
        return self.Reference(**args), ArtifactManifestEntry(**args)

    def equal_exports(self, pair, **kwargs):
        for name in ['model_dump', 'model_dump_json']:
            with self.subTest(method=name, options=kwargs):
                self.assertEqual(getattr(pair[0], name)(**kwargs), getattr(pair[1], name)(**kwargs))

    def test_defaults_aliases_and_explicit_field_tracking(self):
        inputs = [{}, {'ref': None, 'size': None, 'extra': {}, 'skip_cache': False},
                  {'birthArtifactID': 'alias', 'birth_artifact_id': 'ignored'},
                  {'birth_artifact_id': 'native', 'skip_cache': True, 'local_path': ''}]
        options = [{}, {'by_alias': True}, {'by_alias': False}, {'by_alias': None},
                   {'exclude_unset': True}, {'exclude_defaults': True}, {'exclude_none': True},
                   {'exclude_unset': True, 'exclude_none': True, 'exclude_defaults': True}]
        for values in inputs:
            for kwargs in options:
                with self.subTest(values=values):
                    self.equal_exports(self.pair(**values), **kwargs)

    def test_inferred_size_survives_removal_of_local_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source'; path.write_bytes(b'contents')
            for size in [None, 0, 8]:
                pair = self.pair(local_path=path, size=size)
                self.equal_exports(pair, exclude_unset=True)
            pair = self.pair(local_path=path)
            path.unlink()
            self.equal_exports(pair, exclude_unset=True)
            self.assertEqual(pair[1].model_dump(exclude_unset=True)['size'], 8)

    def test_nested_filters_indices_and_field_names(self):
        pair = self.pair(birthArtifactID='origin', extra={
            'rows': [{'a': 1, 'b': 2}, {'a': 3, 'b': 4}], 'tuple': (5, 6), 'other': None})
        options = [
            {'include': {'path', 'birth_artifact_id', 'extra'}},
            {'include': {'birthArtifactID'}},
            {'exclude': {'digest', 'local_path'}},
            {'include': {'extra': {'rows': {0: {'a'}, -1: {'b'}}, 'tuple': {1}}}},
            {'exclude': {'extra': {'rows': {'__all__': {'b'}}, 'tuple': {-1}}}},
            {'include': {'extra': ...}, 'exclude': {'extra': {'rows': {0}, 'other': True}}},
        ]
        for kwargs in options:
            self.equal_exports(pair, **kwargs)

    def test_python_modes_keep_tuples_and_json_modes_convert(self):
        pair = self.pair(extra={'tuple': (1, [2]), 'date': date(2026, 10, 5), 'bytes': b'hello'})
        for mode in ['python', 'json', 'custom-mode']:
            actual = pair[1].model_dump(mode=mode)
            self.assertEqual(actual, pair[0].model_dump(mode=mode))
            self.assertEqual(type(actual['extra']['tuple']), list if mode == 'json' else tuple)
        exported = pair[1].model_dump(mode='python')
        exported['extra']['tuple'][1].append(3)
        self.assertEqual(pair[1].extra['tuple'], (1, [2]))

    def test_nested_custom_serializers_context_computed_fields_and_round_trip(self):
        from pydantic import BaseModel, Json, computed_field, field_serializer
        class Payload(BaseModel):
            value: int
            encoded: Json[list[int]]
            @computed_field
            @property
            def doubled(self) -> int:
                return self.value * 2
            @field_serializer('value')
            def format_value(self, value, info):
                return value * (info.context or {}).get('factor', 1)
        Payload.model_rebuild(_types_namespace={'Json': Json})
        payload = Payload(value=3, encoded='[1,2]')
        pair = self.pair(extra={'payload': payload})
        for kwargs in [{}, {'context': {'factor': 7}}, {'round_trip': False},
                       {'exclude_computed_fields': True, 'round_trip': False},
                       {'context': {'factor': 2}, 'round_trip': False, 'serialize_as_any': True}]:
            self.equal_exports(pair, **kwargs)
        self.assertEqual(pair[1].model_dump()['extra']['payload']['encoded'], '[1,2]')
        self.assertEqual(repr(pair[0]), repr(pair[1]))
        self.assertEqual(pair[0].to_json(), pair[1].to_json())

    def test_nested_model_subclasses_and_runtime_specific_options(self):
        from pydantic import BaseModel
        class User(BaseModel):
            name: str
        class Admin(User):
            level: int
        class Envelope(BaseModel):
            user: User
        Envelope.model_rebuild(_types_namespace={'User': User})
        pair = self.pair(extra={'model': Envelope(user=Admin(name='n', level=2))})
        for flag in [False, True]:
            self.equal_exports(pair, serialize_as_any=flag)
        if 'polymorphic_serialization' in inspect.signature(BaseModel.model_dump).parameters:
            for flag in [False, True, None]:
                self.equal_exports(pair, polymorphic_serialization=flag)

    def test_fallback_unknown_values_and_error_categories(self):
        class Unknown:
            pass
        pair = self.pair(extra={'custom': Unknown()})
        self.equal_exports(pair, fallback=lambda value: {'converted': type(value).__name__})
        for method in ['model_dump', 'model_dump_json']:
            failures = []
            for obj in pair:
                with self.assertRaises(Exception) as caught:
                    getattr(obj, method)()
                failures.append(type(caught.exception))
            self.assertIs(failures[0], failures[1])
        self.assertIs(pair[1].model_dump(mode='python')['extra']['custom'], pair[1].extra['custom'])

    def test_warning_policies_delegate_to_the_same_runtime(self):
        from pydantic import BaseModel
        class Typed(BaseModel):
            count: int
        pair = self.pair(extra={'typed': Typed.model_construct(count='invalid')})
        for method in ['model_dump', 'model_dump_json']:
            for option in [True, False, 'none', 'warn', 'error']:
                outcomes = []
                for entry in pair:
                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter('always')
                        try:
                            result = ('value', getattr(entry, method)(warnings=option))
                        except Exception as error:
                            result = ('error', type(error))
                        outcomes.append((result, [(item.category, str(item.message)) for item in caught]))
                self.assertEqual(outcomes[0], outcomes[1])

    def test_json_formatting_unicode_nonfinite_values_and_rejected_options(self):
        pair = self.pair(extra={'unicode': '雪🌱', 'nan': float('nan'), 'infinity': float('inf')})
        for indent in [None, 0, 2, 4]:
            for ascii_only in [False, True]:
                self.assertEqual(pair[0].model_dump_json(indent=indent, ensure_ascii=ascii_only),
                                 pair[1].model_dump_json(indent=indent, ensure_ascii=ascii_only))
        options = [('model_dump', {'mode': None}), ('model_dump_json', {'indent': -1}),
                   ('model_dump_json', {'indent': 'two'}), ('model_dump', {'warnings': 'invalid'}),
                   ('model_dump', {'include': 5}), ('model_dump_json', {'unknown': True}),
                   ('model_dump', {'by_alias': 'invalid'})]
        for method, kwargs in options:
            outcomes = []
            for entry in pair:
                try:
                    outcomes.append(('value', getattr(entry, method)(**kwargs)))
                except Exception as error:
                    outcomes.append(('error', type(error)))
            self.assertEqual(outcomes[0], outcomes[1], (method, kwargs))
        for method in ['model_dump', 'model_dump_json']:
            for entry in pair:
                with self.assertRaises(TypeError):
                    getattr(entry, method)(2)

    def test_private_native_storage_fields_never_leak_and_bytes_stay_immutable(self):
        import backprop
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source'; path.write_bytes(b'original')
            artifact = backprop.Artifact('serialization', 'dataset')
            entry = artifact.add_file(path, name='file')
            before = artifact.manifest.to_manifest_json()
            public = entry.model_dump()
            self.assertNotIn('sha256', public)
            self.assertNotIn('_parent', public)
            public['digest'] = 'changed'
            self.assertEqual(artifact.manifest.to_manifest_json(), before)
            self.assertEqual(entry._read_verified_bytes(), b'original')
        entry = ArtifactManifestEntry(path='file', digest='', sha256='native',
                                      _parent=object(), _reference={'token': 'private'})
        for method in ['model_dump', 'model_dump_json']:
            exported = getattr(entry, method)()
            self.assertNotIn('private', str(exported)); self.assertNotIn('native', str(exported))

    def test_nested_entries_keep_public_fields_and_serialization_context(self):
        from pydantic import BaseModel
        class Container(BaseModel):
            value: object
        pair = (self.Reference(path='outer', digest='', extra={
                    'child': self.Reference(path='inner', digest='', birthArtifactID='origin')}),
                ArtifactManifestEntry(path='outer', digest='', extra={
                    'child': ArtifactManifestEntry(path='inner', digest='', birthArtifactID='origin',
                                                   _parent=object(), _reference={'secret': 'private'})}))
        for kwargs in [{}, {'exclude_unset': True}, {'by_alias': False},
                       {'include': {'extra': {'child': {'path', 'birth_artifact_id'}}}}]:
            self.equal_exports(pair, **kwargs)
        self.assertEqual(Container(value=pair[0]).model_dump(mode='json'),
                         Container(value=pair[1]).model_dump(mode='json'))

    def test_entry_typed_custom_models_use_the_same_private_field_boundary(self):
        from pydantic import create_model
        pairs = []
        for Entry in [self.Reference, ArtifactManifestEntry]:
            Container = create_model('TypedEntry', entry=(Entry, ...))
            entry = Entry(path='inner', digest='', birthArtifactID='origin')
            if Entry is ArtifactManifestEntry:
                entry = Entry(path='inner', digest='', birthArtifactID='origin',
                              _parent=object(), _reference={'secret': 'private'})
            pairs.append(Entry(path='outer', digest='', extra={'typed': Container(entry=entry)}))
        for kwargs in [{}, {'serialize_as_any': True}, {'by_alias': False},
                       {'exclude_unset': True},
                       {'exclude': {'extra': {'typed': {'entry': {'digest'}}}}}]:
            self.equal_exports(pairs, **kwargs)
        self.assertNotIn('private', pairs[1].model_dump_json())

    def test_new_provenance_is_explicit_without_changing_source_entry(self):
        import backprop
        artifact = backprop.Artifact('provenance', 'dataset')
        source = ArtifactManifestEntry(path='file', digest='')
        copied = artifact._set_entry_provenance(source, ref='backprop-artifact://source/file',
                                                birth_artifact_id='source')
        self.assertEqual(source.model_dump(exclude_unset=True), {'path': 'file', 'digest': ''})
        self.assertEqual(copied.model_dump(exclude_unset=True), {
            'path': 'file', 'digest': '', 'ref': 'backprop-artifact://source/file',
            'birthArtifactID': 'source'})

    def test_alias_and_pinned_exports_in_isolated_processes(self):
        outputs = []
        for kind in ['reference', 'native']:
            result = subprocess.run([sys.executable, '-B', __file__, '--probe', kind],
                                    check=True, capture_output=True, text=True, env=os.environ.copy())
            outputs.append(json.loads(result.stdout))
        self.assertEqual(outputs[0], outputs[1])


def probe(kind):
    """Run alias selection and export in an independent Python interpreter.

    Main callers: isolated-process test above. The pinned model has no hosted
    dependency in these operations, so no W&B authentication/service is started.
    """
    if kind == 'reference':
        Entry = checked_reference()
    else:
        from backprop.compatibility import install_wandb_aliases
        install_wandb_aliases()
        from wandb.sdk.artifacts.artifact_manifest_entry import ArtifactManifestEntry as Entry
        assert Entry is ArtifactManifestEntry
    entry = Entry(path='file', digest='', birthArtifactID='origin', extra={'values': (1, 2)})
    return [entry.model_dump(), entry.model_dump(exclude_unset=True),
            entry.model_dump_json(indent=2), entry.model_dump(by_alias=False, exclude_none=True)]


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(probe(sys.argv[2])))
    else:
        unittest.main()
