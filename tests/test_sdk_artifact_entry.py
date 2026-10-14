"""Portable manifest-entry behavior compared with pinned W&B source.

Architecture: public assertions cover Backprop -> installed
Pydantic executes the unchanged pinned class/validators/custom repr -> identical
inputs compare return values and public exception categories. W&B networking,
telemetry and storage adapters are replaced only at their external boundaries.
No packages, user configuration, credentials or network requests are needed.
"""
from __future__ import annotations

import ast
import base64
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import importlib.util
import inspect
from ipaddress import IPv4Address
import os
import re
from pathlib import Path, PureWindowsPath
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

import backprop
from backprop.artifacts import ArtifactManifestEntry, ArtifactDigestAlgorithm


SOURCE_ROOT = Path(__file__).resolve().parents[1] / '.reference/wandb/source/wandb'


def reference_entry():
    """Execute original model code with the installed validation runtime.

    Main callers: differential tests. The original field decorators and custom
    methods are unchanged. Only imported W&B services are supplied as inert
    stand-ins, while the genuine pinned JsonableModel and LogicalPath execute.
    """
    import typing
    from abc import ABC
    import pydantic
    import typing_extensions
    def module(name, path):
        result = ModuleType(name)
        sys.modules[name] = result
        exec(compile(path.read_text(), str(path), 'exec'), result.__dict__)
        return result
    base = module('backprop_entry_reference_base', SOURCE_ROOT / '_pydantic/base.py')
    paths = module('backprop_entry_reference_paths', SOURCE_ROOT / 'sdk/lib/paths.py')
    strings = module('backprop_entry_reference_strings', SOURCE_ROOT / '_strutils.py')
    owner = ModuleType('backprop_entry_reference')
    sys.modules[owner.__name__] = owner
    owner.__dict__.update(vars(typing))
    owner.__dict__.update(ABC=ABC, ConfigDict=pydantic.ConfigDict, JsonableModel=base.JsonableModel)
    class_source = SOURCE_ROOT / 'sdk/artifacts/_models/base_model.py'
    entry_source = SOURCE_ROOT / 'sdk/artifacts/artifact_manifest_entry.py'
    nodes = []
    for path, class_name in [(class_source,'ArtifactsBase'),(entry_source,'ArtifactManifestEntry')]:
        nodes.append(next(node for node in ast.parse(path.read_text()).body if isinstance(node,ast.ClassDef) and node.name==class_name))
    owner.__dict__.update(Field=pydantic.Field, NonNegativeInt=pydantic.NonNegativeInt,
        field_validator=pydantic.field_validator, model_validator=pydantic.model_validator,
        Self=typing_extensions.Self, LogicalPath=paths.LogicalPath, B64Digest=str, ETag=str,
        URIStr=str, FilePathStr=str, Artifact=object, getsize=os.path.getsize, nameof=strings.nameof,
        ArtifactDigestAlgorithm=ArtifactDigestAlgorithm,
        DIGEST_ALGORITHM_EXTRA_KEY='alg', _STR_TO_DIGEST_ALGORITHM={'MD5':ArtifactDigestAlgorithm.MANIFEST_MD5,'XXH128':ArtifactDigestAlgorithm.MANIFEST_XXH128},
        warn_and_record_deprecation=lambda **kwargs: None, Deprecated=lambda **kwargs: None)
    tree=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree),str(entry_source),'exec'),owner.__dict__)
    owner.ArtifactManifestEntry.model_rebuild(_types_namespace=owner.__dict__)
    return owner


class EntryContracts(unittest.TestCase):
    def test_custom_repr_hides_native_fields_and_omits_only_none_empty_extra(self):
        entry=ArtifactManifestEntry(path='a//file.txt',digest='',sha256='native-only',_parent=object(),_reference={'token':'private'})
        self.assertEqual(repr(entry),"ArtifactManifestEntry(path='a/file.txt', digest='', skip_cache=False)")
        richer=ArtifactManifestEntry(path='a',digest='d',ref='',birthArtifactID='origin',size=0,
            extra={'nested':(1,None)},skip_cache=True)
        self.assertEqual(repr(richer),"ArtifactManifestEntry(path='a', digest='d', ref='', birth_artifact_id='origin', size=0, extra={'nested': [1, None]}, skip_cache=True)")
        self.assertNotIn('native-only',repr(entry));self.assertNotIn('private',repr(entry))

    def test_keyword_signature_defaults_alias_priority_and_independent_extra(self):
        with self.assertRaises(TypeError):ArtifactManifestEntry('file','digest')
        signature=inspect.signature(ArtifactManifestEntry)
        self.assertEqual(list(signature.parameters),['path','digest','ref','birthArtifactID','size','extra','local_path','skip_cache'])
        self.assertTrue(all(value.kind is inspect.Parameter.KEYWORD_ONLY for value in signature.parameters.values()))
        first=ArtifactManifestEntry(path='first',digest='',birth_artifact_id='loses',birthArtifactID='wins',unknown='ignored')
        second=ArtifactManifestEntry(path='second',digest='')
        self.assertEqual(first.birth_artifact_id,'wins');self.assertIsNot(first.extra,second.extra)
        self.assertFalse(hasattr(first,'unknown'));self.assertFalse(second.skip_cache)
        with self.assertRaises(TypeError):first.extra['changed']=1

    def test_local_size_inference_and_parent_reference_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source';source.write_bytes(b'content')
            entry=ArtifactManifestEntry(path=Path('file'),digest='',local_path=source)
            self.assertEqual(entry.size,7);self.assertEqual(entry.local_path,str(source))
            self.assertEqual(ArtifactManifestEntry(path='file',digest='',local_path=source,size='3.0').size,3)
            with self.assertRaises(FileNotFoundError):ArtifactManifestEntry(path='file',digest='',local_path=source.with_name('missing'))
        for method in [entry.parent_artifact,entry.download,entry.ref_url]:
            with self.assertRaises(NotImplementedError) as caught:method()
            self.assertEqual(str(caught.exception),'')
        with self.assertRaisesRegex(ValueError,r'Only reference entries support ref_target\(\)\.'):entry.ref_target()
        missing_id=ArtifactManifestEntry(path='file',digest='',_parent=SimpleNamespace(id=None))
        with self.assertRaisesRegex(ValueError,'^Parent artifact ID is not set$'):missing_id.ref_url()
        with patch.object(backprop,'termwarn') as warning:
            self.assertIs(entry.name,entry.path)
            warning.assert_called_once_with('ArtifactManifestEntry.name is deprecated, use .path instead.',repeat=False)

    def test_public_json_aliases_stay_separate_from_native_manifest_storage(self):
        entry=ArtifactManifestEntry(path='file',digest='abc',birth_artifact_id='origin',sha256='native',size=3,local_path='source',skip_cache=True)
        public=entry.to_json()
        self.assertEqual(public,{'path':'file','digest':'abc','birthArtifactID':'origin','size':3,'extra':{},'local_path':'source','skip_cache':True})
        artifact=backprop.Artifact('entry-test','dataset');artifact._entries['file']=entry
        native=artifact.manifest.to_manifest_json()['contents']['file']
        self.assertEqual(native['birth_artifact_id'],'origin');self.assertEqual(native['sha256'],'native')
        self.assertNotIn('local_path',native);self.assertNotIn('skip_cache',native)

    def test_valid_existing_file_reused_even_when_skip_cache_requested_or_size_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            destination=Path(directory)/'file';destination.write_bytes(b'correct')
            digest=base64.b64encode(hashlib.md5(b'correct').digest()).decode()
            owner=SimpleNamespace(_default_root=lambda:directory)
            for setting in [False,True]:
                for requested in [None,False,True]:
                    entry=ArtifactManifestEntry(path='file',digest=digest,skip_cache=setting,_parent=owner)
                    with patch.object(ArtifactManifestEntry,'_read_verified_bytes',side_effect=AssertionError('existing bytes must be reused')):
                        self.assertEqual(entry.download(skip_cache=requested),str(destination.resolve()))

    def test_destination_without_any_expected_checksum_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            destination=Path(directory)/'file';destination.write_bytes(b'unknown')
            entry=ArtifactManifestEntry(path='file',digest='',_parent=SimpleNamespace(_default_root=lambda:directory))
            with patch.object(ArtifactManifestEntry,'_read_verified_bytes',return_value=b'source') as read:
                entry.download(skip_cache=True)
                read.assert_called_once_with()
            self.assertEqual(destination.read_bytes(),b'source')


@unittest.skipUnless(importlib.util.find_spec('pydantic') and SOURCE_ROOT.exists(),'Optional installed Pydantic and pinned source required for differential test')
class PinnedEntryContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference=reference_entry()
        cls.Entry=cls.reference.ArtifactManifestEntry

    def test_custom_repr_and_json_match_original_pinned_method(self):
        class Choice(str,Enum):VALUE='selected'
        @dataclass
        class Datum:
            value: int
        cases=[{}, {'path':Path('a/../b')},{'path':b'a//b/'},{'path':PureWindowsPath('folder/file')},
            {'birthArtifactID':'alias','birth_artifact_id':'other','ref':'','size':0,'skip_cache':'yes'},
            {'extra':{'tuple':(1,2),'nested':{'flag':True},'date':date(2026,10,3),'time':datetime(2026,10,3,tzinfo=timezone.utc),
                      'bytes':b'hello','enum':Choice.VALUE,'decimal':Decimal('1.25'),'uuid':UUID(int=1),'path':Path('a/b')}},
            {'extra':{'clock':time(1,2,tzinfo=timezone.utc), 'durations':[timedelta(),timedelta(days=1),timedelta(days=-1,seconds=2,microseconds=1200)],
                      'nested':{None:0,True:1,(1,2):'tuple',b'a':2},'address':IPv4Address('127.0.0.1'),'pattern':re.compile('abc'),'datum':Datum(1)}},
            {'extra':{b'key':(1,2)}}, {'extra':OrderedDict([('b',1),('a',2)])}]
        for changes in cases:
            with self.subTest(changes=changes):
                args={'path':'file','digest':'abc',**changes}
                upstream=self.Entry(**args);native=ArtifactManifestEntry(**args)
                for operation in [repr, lambda value: value.to_json()]:
                    outcomes=[]
                    for value in [upstream,native]:
                        try:outcomes.append(('value',operation(value)))
                        except ValueError as error:outcomes.append(('error',type(error)))
                    self.assertEqual(outcomes[0],outcomes[1])
        native_child=type('Nested.Entry',(ArtifactManifestEntry,),{})
        upstream_child=type('Nested.Entry',(self.Entry,),{})
        self.assertEqual(repr(native_child(path='p',digest='')),repr(upstream_child(path='p',digest='')))

    def test_constructor_coercions_and_rejection_categories_match(self):
        cases=[{}, {'path':None},{'path':4},{'digest':None},{'digest':123},{'digest':b'valid'},{'digest':b'\xff'},
            {'ref':Path('file')},{'ref':b'file:///a'}, {'birth_artifact_id':1}, {'unknown_field':True},
            *[{'size':value} for value in [True,False,0,'3','3.0',' 3 ','+03','1_000','1e2','3.', '.0','１',1.5,-1,Decimal('2.0'),float('inf')]],
            *[{'skip_cache':value} for value in [True,False,0,1,1.0,1.5,'off','ON','yes','n',b'TRUE',None,2,object()]],
            *[{'extra':value} for value in [None,[],[('a',1)],{1:'bad'},{'ok':1},{b'ok':1}]],
            {'local_path':0}, {'local_path':1}]
        def outcome(kind,args):
            try:return ('value',kind(**args).to_json())
            except (TypeError,ValueError,OSError) as error:
                category=TypeError if isinstance(error,TypeError) else ValueError if isinstance(error,ValueError) else type(error)
                return ('error',category)
        for changes in cases:
            with self.subTest(changes=changes):
                args={'path':'file','digest':'',**changes}
                self.assertEqual(outcome(ArtifactManifestEntry,args),outcome(self.Entry,args))
        for args in [{},{'path':'file'},{'digest':'value'}]:
            self.assertEqual(outcome(ArtifactManifestEntry,args),outcome(self.Entry,args))
        with self.assertRaises(TypeError):self.Entry('file','digest')

    def test_numeric_protocol_validation_matches_pinned_models(self):
        class FloatOne:
            def __float__(self):return 1.0
        class IndexOne:
            def __index__(self):return 1
        class IntOnly:
            def __int__(self):return 1
        class BoolOnly:
            def __bool__(self):return True
        class Fractional:
            def __float__(self):return 1.5
        values=[FloatOne(),IndexOne(),IntOnly(),BoolOnly(),Fractional()]
        if importlib.util.find_spec('numpy'):
            import numpy as np
            values.extend([np.bool_(True),np.int64(1),np.float64(1),np.array(1),np.array([1])])
        for name in ['size','skip_cache']:
            for value in values:
                with self.subTest(field=name,type=type(value).__name__):
                    outcomes=[]
                    for kind in [self.Entry,ArtifactManifestEntry]:
                        try:outcomes.append(('value',getattr(kind(path='file',digest='',**{name:value}),name)))
                        except ValueError:outcomes.append(('error',ValueError))
                    self.assertEqual(*outcomes)

    def test_path_operations_and_public_reference_errors_match(self):
        for path in ['a/./b','folder//file','',Path('a/b'),PureWindowsPath('a/b'),b'a/b']:
            with self.subTest(path=path):
                native=ArtifactManifestEntry(path=path,digest='').path;upstream=self.Entry(path=path,digest='').path
                self.assertEqual(native,upstream);self.assertEqual(native.parts,upstream.parts)
                self.assertEqual(native.parent/'next',upstream.parent/'next')
                outcomes=[]
                for value in [native,upstream]:
                    try:outcomes.append(('value',value.with_suffix('.json')))
                    except ValueError as error:outcomes.append(('error',type(error),str(error)))
                self.assertEqual(*outcomes)
        for method in ['parent_artifact','ref_target','ref_url','download']:
            results=[]
            for cls in [self.Entry,ArtifactManifestEntry]:
                try:getattr(cls(path='file',digest=''),method)()
                except Exception as error:results.append((type(error),str(error)))
            self.assertEqual(results[0],results[1])

    def test_pinned_download_reuses_valid_destination_before_backend_cache_flag(self):
        from contextlib import suppress
        reference=self.reference
        reference.__dict__.update(suppress=suppress,validate_fspath=lambda root,path:os.path.join(root,path),
            _read_cached_checksum=lambda path:None,_write_cached_checksum=lambda *args:None,
            md5_file_b64=lambda path:base64.b64encode(hashlib.md5(Path(path).read_bytes()).digest()).decode(),
            logger=SimpleNamespace(debug=lambda *args:None))
        with tempfile.TemporaryDirectory() as directory:
            directory=str(Path(directory).resolve())
            path=Path(directory)/'file';path.write_bytes(b'correct')
            digest=base64.b64encode(hashlib.md5(b'correct').digest()).decode()
            for flag in [False,True]:
                upstream=self.Entry(path='file',digest=digest,skip_cache=flag)
                upstream._parent_artifact=SimpleNamespace(_add_download_root=lambda root:directory)
                native=ArtifactManifestEntry(path='file',digest=digest,skip_cache=flag,_parent=SimpleNamespace(_default_root=lambda:directory))
                for override in [None,False,True]:
                    self.assertEqual(native.download(skip_cache=override),upstream.download(skip_cache=override))


if __name__=='__main__':unittest.main()
