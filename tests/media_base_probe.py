"""Isolated base-media comparison against the installed pinned W&B wheel.

Architecture: same file fixture -> constructors, binding modes and serialization
-> portable observations. A fake run only records _publish_file calls; binding
itself executes real upstream/native code and writes actual files.
Main caller: test_sdk_media_base. No hosted W&B service is used.
"""
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

if sys.argv[1]=="reference":
    import wandb as w
    from wandb.sdk.data_types.base_types.media import Media, BatchableMedia
    assert w.__version__=="0.30.0"
    assert hashlib.sha256(Path(inspect.getfile(Media)).read_bytes()).hexdigest()=="9c2ea72e84eb2b07c91ddaa4e5ca94e70aff0c25ede272bf5ccc257efe61f856"
else:
    import backprop as w
    from backprop.media import Media, BatchableMedia

class FileMedia(Media):
    _log_type="base-probe"
    def __init__(self,path,caption=None,temporary=False,extension=None):
        super().__init__(caption);self._set_file(str(path),temporary,extension)
    @classmethod
    def get_media_subdir(cls):return "media/probe"

def error_of(call):
    try:return call()
    except Exception as error:return type(error).__name__

result={"constructors":[],"binding":[],"references":[]}
for cls in (Media,BatchableMedia):
    empty=cls(caption="hello")
    result["constructors"].append({"caption":empty._caption,"bound":empty.is_bound(),"file":empty.file_is_set(),
        "directory":error_of(cls.get_media_subdir),"array":error_of(empty.to_data_array),
        "captions":[cls.captions([cls("a"),cls()]),cls.captions([cls(),cls("b")])],
        "empty_captions":error_of(lambda:cls.captions([])),"suffix":cls.with_suffix("thing")})
result["abstract_batch"]=error_of(lambda:BatchableMedia.seq_to_json([],None,"x",0))
for path in (None,Path("https://example.com"),"https://example.com/a","http://a","s3://a","gs://a","ftp://a","file:///tmp/a",""):
    result["references"].append(Media.path_is_reference(path))
with tempfile.TemporaryDirectory() as directory:
    directory=Path(directory)
    for i,(link,symlink,temporary,extension) in enumerate([(False,True,False,None),(True,True,False,None),(True,False,False,None),(False,True,True,None),(False,True,False,"")]):
        src=directory/f"source{i}.bin";src.write_bytes(b"payload")
        published=[];settings=SimpleNamespace(allow_media_symlink=link,symlink=symlink)
        run=SimpleNamespace(dir=str(directory/f"run{i}"),_settings=settings,settings=settings,_publish_file=published.append)
        value=FileMedia(src,caption="bound",temporary=temporary,extension=extension)
        returned=value.bind_to_run(run,"/../nested/key",7,id_=[None,0,"chosen",3,3][i])
        dest=Path(value._path)
        result["binding"].append({"return":returned,"path":dest.relative_to(run.dir).as_posix(),"published":published,
            "bytes":dest.read_text(),"symlink":dest.is_symlink(),"source_exists":src.exists(),"bound":value.is_bound(),
            "hash":value._sha256,"size":value._size,"file":value.file_is_set(),"temporary":value._is_tmp})
        if not link:
            value.bind_to_run(run,"same",0,id_="fixed")
            result["binding"][-1]["same_error"]=error_of(lambda:value.bind_to_run(run,"same",0,id_="fixed"))
            result["binding"][-1]["ignore_same"]=error_of(lambda:value.bind_to_run(run,"same",0,id_="fixed",ignore_copy_err=True))
    src=directory/"artifact.bin";src.write_bytes(b"artifact")
    value=FileMedia(src,caption="saved")
    artifact=w.Artifact("base-probe","dataset")
    result["artifact"]=value.to_json(artifact)
    result["artifact_repeated"]=value.to_json(artifact)
    result["empty_artifact"]=Media("empty").to_json(artifact)
    restored=Media.from_json({"path":"x"},SimpleNamespace(get_entry=lambda path:SimpleNamespace(download=lambda:"saved-path")))
    result["base_from_json"]={"caption":restored._caption,"file":restored.file_is_set()}
    result["bad_extension"]=error_of(lambda:FileMedia(src,extension=".png"))
    result["missing_run"]=error_of(lambda:FileMedia(src).bind_to_run(None,"x",0))
    result["unset_bind"]=error_of(lambda:Media().bind_to_run(None,"x",0))
    result["equal"]=value==FileMedia(src)
print(json.dumps(result,sort_keys=True))
