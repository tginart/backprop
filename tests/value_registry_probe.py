"""Compare the unchanged pinned WBValue registry with native local extensions.

Architecture: separate subprocess -> source-backed base class or native class ->
identical extension declarations and operations -> portable JSON observations.
Main caller: test_sdk_value_registry. No network or optional dependency is used.
"""
import ast
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

root=Path(__file__).resolve().parents[1]
if sys.argv[1]=="reference":
    path=root/".reference/wandb/source/wandb/sdk/data_types/base_types/wb_value.py"
    source=path.read_bytes()
    assert hashlib.sha256(source).hexdigest()=="083f4db030911ce160d991bdbd61542f1ffa6f8c90925a948381bc90e7500ae2"
    cls=next(node for node in ast.parse(source).body if isinstance(node,ast.ClassDef) and node.name=="WBValue")
    methods={"__init__","to_json","from_json","with_suffix","type_mapping","init_from_json","_set_artifact_source"}
    cls.body=[node for node in cls.body if isinstance(node,(ast.Assign,ast.AnnAssign)) or isinstance(node,ast.FunctionDef) and node.name in methods]
    tree=ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__",names=[ast.alias(name="annotations")],level=0),cls],type_ignores=[]))
    namespace={"_WBValueArtifactSource":lambda artifact,name=None:SimpleNamespace(artifact=artifact,name=name)}
    exec(compile(tree,str(path),"exec"),namespace)
    WBValue=namespace["WBValue"]
else:
    from backprop.values import WBValue

WBValue._type_mapping=None
class Parent(WBValue):
    _log_type="registry-probe"
    def __init__(self,value):super().__init__();self.value=value
    def to_json(self,owner):return {"_type":self._log_type,"value":self.value}
    @classmethod
    def from_json(cls,value,owner):return cls(value["value"])
class Child(Parent):pass
mapping=WBValue.type_mapping()
first=mapping["registry-probe"].__name__
class Later(Parent):_log_type="registry-later"
cache_stable=WBValue.type_mapping() is mapping and "registry-later" not in mapping
mapping["registry-manual"]=Parent
manual=WBValue.init_from_json({"_type":"registry-manual","value":3},None)
WBValue._type_mapping=None
refreshed=WBValue.type_mapping()["registry-later"].__name__
owner=SimpleNamespace(id="artifact")
value=WBValue.init_from_json({"_type":"registry-probe","value":4},owner)
source=value._artifact_source
source_matches=(source is owner if sys.argv[1]!="reference" else source.artifact is owner)
errors=[]
for call in (lambda:WBValue.init_from_json({},owner),lambda:WBValue.init_from_json({"_type":[]},owner),lambda:WBValue.from_json({},owner),lambda:WBValue().to_json(owner)):
    try:call()
    except Exception as error:errors.append(type(error).__name__)
print(json.dumps({"first":first,"cache":cache_stable,"manual":manual.value,"later":refreshed,
                  "value":value.value,"source":source_matches,"errors":errors,
                  "unknown":WBValue.init_from_json({"_type":"registry-unknown"},owner),
                  "suffixes":[Parent.with_suffix(name,filetype) for name in ("x","x.registry-probe.json","") for filetype in ("json","txt","")]}))
