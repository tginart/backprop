"""Isolated Audio/Video public API comparison with pinned W&B 0.30.0.

Architecture: real encoded files -> public constructors and alias calls -> file
binding and artifact descriptors -> input/error matrices. A recording run only
replaces service publication; actual file and manifest code executes unchanged.
Raw-audio/sequence cases run when the optional reference SoundFile exists.
Main caller: test_optional_audio_video. No service or credentials are needed.
"""
import base64
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import wave

reference = sys.argv[1] == "reference"
if reference:
    import wandb as w
    assert w.__version__ == "0.30.0"
else:
    import backprop as w
    from backprop.compatibility import install_wandb_aliases
    install_wandb_aliases()
SOUNDFILE = importlib.util.find_spec("soundfile") is not None


def outcome(call):
    try: return {"value": call()}
    except Exception as error: return {"error": type(error).__name__}


def signature(call):
    return [(p.name, p.kind.name, "required" if p.default is p.empty else repr(p.default))
            for p in inspect.signature(call).parameters.values()]


class ProbeRun:
    def __init__(self, directory, link=False, symlink=False):
        self.dir = str(directory); self.published = []
        self._settings = SimpleNamespace(allow_media_symlink=link, symlink=symlink)
    def _publish_file(self, path): self.published.append(path)


out = {"soundfile": SOUNDFILE, "aliases": {}, "classes": {}}
with tempfile.TemporaryDirectory() as directory:
    directory = Path(directory)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000); wav.writeframes(b"\0\0\1\0")
    audio_path = directory / "source.wav"; audio_path.write_bytes(buffer.getvalue())
    video_path = directory / "source.webm"; video_path.write_bytes(b"encoded-video")
    boundary = patch.object(w, "Run", ProbeRun) if reference else patch.object(importlib.import_module("backprop.run"), "Run", ProbeRun)
    with boundary:
        for cls, source_path in [(w.Audio, audio_path), (w.Video, video_path)]:
            name = cls.__name__
            if reference: out.setdefault("source_hashes", {})[name] = hashlib.sha256(Path(inspect.getfile(cls)).read_bytes()).hexdigest()
            def make(cls=cls, path=source_path, generated=False, caption="caption"):
                if generated:
                    if name == "Audio": return cls([0., .5, -.5], sample_rate=8000, caption=caption)
                    return cls(io.BytesIO(b"encoded-video"), format="webm", caption=caption)
                return cls(path, caption=caption)
            methods = ["__init__", "bind_to_run", "file_is_set", "is_bound", "path_is_reference", "to_json", "from_json", "seq_to_json", "captions", "get_media_subdir"]
            if name == "Audio": methods += ["durations", "sample_rates", "resolve_ref"]
            for module in ["wandb", "wandb.data_types", "wandb.sdk.data_types." + name.lower()]:
                alias = getattr(importlib.import_module(module), name)
                value = make(cls=alias); run = ProbeRun(directory / (name + module)); artifact = w.Artifact("alias-" + name.lower(), "dataset")
                value.bind_to_run(run, "alias", 2, id_="fixed")
                descriptor = value.to_json(run=run) if name == "Audio" else value.to_json(run_or_artifact=run)
                saved = value.to_json(artifact)
                restored = alias.from_json(json_obj=saved, source_artifact=SimpleNamespace(get_entry=lambda p: SimpleNamespace(download=lambda: value._path)))
                row = {"same_class": alias is cls, "signatures": {m: signature(getattr(alias, m)) for m in methods},
                    "file": value.file_is_set(), "bound": value.is_bound(), "json": descriptor, "artifact": saved,
                    "decoded": {"file": restored.file_is_set(), "bound": restored.is_bound(), "caption": restored._caption,
                                "bytes": base64.b64encode(Path(restored._path).read_bytes()).decode()},
                    "paths": [alias.path_is_reference("https://a"), alias.path_is_reference("ftp://a")], "captions": alias.captions([value]),
                    "directory": alias.get_media_subdir()}
                if name != "Audio" or SOUNDFILE: row["batch"] = alias.seq_to_json([value], run, "x", 2)
                if name == "Audio": row.update(rates=alias.sample_rates([value]), durations=alias.durations([value]), resolve=outcome(value.resolve_ref))
                out["aliases"][module + "." + name] = row
            rows=[];out["classes"][name]={"binding": rows}
            for generated in ([False,True] if name != "Audio" or SOUNDFILE else [False]):
                for i, (identity, ignore, link, symlink) in enumerate([(None,None,False,False),(0,False,False,False),("chosen",True,False,False),("hardlink",True,True,False),("symlink",True,True,True)]):
                    value=make(generated=generated);original=Path(value._path)
                    run=ProbeRun(directory/f"{name}-{generated}-{i}",link,symlink); other=ProbeRun(directory/f"{name}-{generated}-{i}-other")
                    row={"generated":generated,"before":[value.file_is_set(),value.is_bound()],"unbound":outcome(lambda:value.to_json(run)),"missing_owner":outcome(value.to_json),"invalid_owner":value.to_json(None)}
                    row["return"]=value.bind_to_run(run,"/../nested/key","step",id_=identity,ignore_copy_err=ignore)
                    row.update(json=value.to_json(run),published=list(run.published),source_exists=original.exists(),symlink=Path(value._path).is_symlink(),bytes=base64.b64encode(Path(value._path).read_bytes()).decode(),bound=value.is_bound())
                    row["same_file"]=outcome(lambda:value.bind_to_run(run,"/../nested/key","step",id_=identity,ignore_copy_err=ignore))
                    row["ignore_same"]=value.bind_to_run(run,"/../nested/key","step",id_=identity,ignore_copy_err=True)
                    row["wrong_owner"]=outcome(lambda:value.to_json(other))
                    artifact=w.Artifact(f"{name.lower()}-{generated}-{i}","dataset");row["artifact"]=value.to_json(artifact);row["repeat"]=value.to_json(artifact)
                    value.bind_to_run(other,"cross",9,id_="cross");row["cross"]=value.to_json(other);row["old_owner"]=outcome(lambda:value.to_json(run));rows.append(row)
            state=out["classes"][name];value=make();run=ProbeRun(directory/(name+'-helpers'));value.bind_to_run(run,'value',0)
            state["paths"]=[outcome(lambda p=p:cls.path_is_reference(p)) for p in [None,Path("https://a"),"s3://a","gs://a","http://a","https://a","ftp://a","file:///a","",{},1,b"https://a"]]
            state["captions"]=[outcome(lambda seq=seq:cls.captions(seq)) for seq in [[],[make(caption=None),make(caption="second")],[make(caption="first"),make(caption=None)],[make(caption=0),make(caption=False)],[object()]]]
            if name != "Audio" or SOUNDFILE:
                state["batches"]=[outcome(lambda seq=seq:cls.seq_to_json(seq,run,'x',0)) for seq in [[],[value],[value,object()],None,iter([value])]]
                state["batch_directory"]=Path(run.dir,cls.get_media_subdir()).is_dir()
            calls=[]
            def download(path):
                calls.append(path)
                if path=='missing': raise FileNotFoundError(path)
                return SimpleNamespace(download=lambda:str(source_path))
            owner=SimpleNamespace(get_entry=download)
            def decode(data):
                item=cls.from_json(data,owner)
                return {'caption':item._caption,'file':item.file_is_set(),'bound':item.is_bound()}
            state["decode"]=[outcome(lambda v=v:decode(v)) for v in [{},{'path':'saved'},{'path':'saved','caption':None},{'path':'saved','caption':'caption'},{'path':'missing'},None,[]]]
            state["download_calls"]=calls
            if name=='Audio':
                state["rates"]=[cls.sample_rates([cls(source_path,sample_rate=rate)]) for rate in [None,0,8000,16000]]
                state["durations"]=[cls.durations([cls(source_path,sample_rate=rate)]) for rate in [None,0,8000,16000]]
                state["uri"]=[]
                for uri in ['https://example.invalid/audio.wav','s3://bucket/file.wav','gs://bucket/file.wav']:
                    item=cls(uri,caption='remote')
                    state["uri"].append({'file':item.file_is_set(),'bound':item.is_bound(),'ref':item.resolve_ref(),'bind':outcome(lambda:item.bind_to_run(run,'uri',0))})
        # Provider lookup is a storage fixture; actual public serializers and
        # resolve_ref execute unchanged. Foundry owns the native artifact entry.
        out["audio_provider_artifact"]=[]
        for uri in ['https://example.invalid/a.wav','https://example.invalid/a.wav?revision=2']:
            value=w.Audio(uri,caption='provider');artifact=w.Artifact('provider','dataset');calls=[]
            def add_reference(uri,name=None):
                calls.append([uri,name]);return []
            with patch.object(artifact,'add_reference',side_effect=add_reference):
                result=value.to_json(artifact)
            out["audio_provider_artifact"].append({'json':result,'calls':calls})
        out["audio_resolved_sources"]=[]
        for logical, entry in [(None,None),('file.wav',None),('file.wav',SimpleNamespace(ref=None)),('file.wav',SimpleNamespace(ref='s3://bucket/audio.wav'))]:
            source=SimpleNamespace(_local_path_to_name=lambda p:logical,get_added_local_path_name=lambda p:logical,
                manifest=SimpleNamespace(get_entry_by_path=lambda p:entry))
            value=w.Audio(audio_path)
            value._artifact_source=SimpleNamespace(artifact=source) if reference else source
            out["audio_resolved_sources"].append(value.resolve_ref())
        out['audio_equality']={}
        left=w.Audio(audio_path,caption='caption')
        for name,right in [('none',None),('object',object()),('same',w.Audio(audio_path,caption='caption')),('caption',w.Audio(audio_path,caption='different'))]:
            out['audio_equality'][name]=outcome(lambda right=right:left==right)
print(json.dumps(out,sort_keys=True))
