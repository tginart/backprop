"""Execute the pinned Gym adapter unchanged with controlled recorder boundaries.

Architecture: separate process -> hashed upstream source or native adapter ->
identical legacy recorder events. Modern/old-library failures are recorded as
reference bugs, never used as the desired recording behavior.
Main caller: test_sdk_gym; real encoder/version coverage lives in CI.
"""
import ast
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from gym_fixtures import library
root=Path(__file__).resolve().parents[1]
backend,name,version=sys.argv[1:]
modules=library(name,version)
logged=[]
class Run:
    _closed=False
    def log(self, data):logged.append(data)
run=Run()
if backend=="reference":
    path=root/".reference/wandb/source/wandb/integration/gym/__init__.py"
    source=path.read_bytes()
    assert hashlib.sha256(source).hexdigest()=="008ed043e74b6b88c48de21bc1b3f8a1cf11ba0590f3d94c9488334ac3bac733"
    tree=ast.parse(source)
    tree.body=[node for node in tree.body if not isinstance(node,ast.Import) or not any(a.name.startswith("wandb") for a in node.names)]
    # The fixture uses release-only version strings. This dependency stand-in
    # exercises the upstream branch selection without requiring packaging in
    # the stdlib-only contract job; it does not test packaging itself.
    packaging=ModuleType("packaging");packaging.__path__=[]
    version_module=ModuleType("packaging.version")
    version_module.parse=lambda value:tuple(int(v) for v in value.replace("a1", "").split("."))
    modules.update({"packaging":packaging,"packaging.version":version_module})
    api=SimpleNamespace(run=run,log=run.log,Video=str,Error=RuntimeError,patched={"gym":[]})
    api.util=SimpleNamespace(get_module=lambda name,**kwargs:modules.get(name))
    namespace={"wandb":api}
    exec(compile(tree,str(path),"exec"),namespace)
    monitor=namespace["monitor"]
else:
    import backprop as api
    from backprop.integration.gym import monitor
    api.run=run;api.Video=str
with patch.dict(sys.modules,modules), tempfile.TemporaryDirectory() as folder:
    monitor_result=monitor()
    recorder_module=modules[name+".wrappers.monitoring.video_recorder"]
    error=None
    if name=="gymnasium" and int(version.split(".")[0])>=1:
        recorder=modules[name+".wrappers"].RecordVideo(folder)
        recorder.start_recording("rl-video-episode-0");recorder.recorded_frames=[1]
    elif backend=="reference" and version.startswith("0.25"):
        recorder=recorder_module.ImageEncoder(Path(folder)/"rl-video-episode-0.mp4")
    else:
        recorder=recorder_module.VideoRecorder(Path(folder)/"rl-video-episode-0.mp4")
    try: recorder.close()
    except Exception as e:error=type(e).__name__
    if error is None and not(name=="gymnasium" and int(version.split(".")[0])>=1):
        recorder=recorder_module.VideoRecorder(Path(folder)/"openai.video.17.mp4");recorder.close()
        recorder=recorder_module.VideoRecorder(Path(folder)/"disabled.mp4",enabled=False);recorder.close()
        api.run=None
        recorder=recorder_module.VideoRecorder(Path(folder)/"inactive.mp4");recorder.close()
    print(json.dumps({"return":monitor_result,"error":error,"logs":[{k:Path(v).name for k,v in row.items()} for row in logged]}))
