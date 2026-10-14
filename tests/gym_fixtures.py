"""Small recorder fixtures for hook boundaries, not video-encoding substitutes.

Architecture: Gym-shaped modules expose legacy close or modern stop_recording;
CI's optional tests separately exercise real installed encoders and wrappers.
Main callers: isolated pinned comparison and deterministic failure/cleanup tests.
"""
from pathlib import Path
from types import ModuleType


def library(name="gymnasium", version="1.0.0"):
    package = ModuleType(name); package.__path__ = []; package.__version__ = version
    wrappers = ModuleType(name + ".wrappers"); wrappers.__path__ = []
    module = ModuleType(name + ".wrappers.monitoring.video_recorder")

    class VideoRecorder:
        def __init__(self, path, enabled=True, empty=False, broken=False):
            self.path=str(path); self.enabled=enabled; self.empty=empty; self.broken=broken
            self._closed=False; self.closes=0; self.fail=False
        def close(self):
            self.closes += 1
            if self.fail: raise OSError("encoder failed")
            if (not self._closed and self.enabled and not getattr(self,"empty",False) and not self.broken
                    and (not hasattr(self,"recorded_frames") or bool(self.recorded_frames))):
                Path(self.path).write_bytes(b"legacy-mp4")
            self._closed=True
        def __del__(self): self.close()

    class RecordVideo:
        def __init__(self, folder):
            self.video_folder=str(folder); self.recording=False; self.recorded_frames=[]
            self._video_name=None; self.closes=0; self.fail=False
        def start_recording(self, video_name):
            if self.recording: self.stop_recording()
            self.recording=True; self._video_name=video_name; self.recorded_frames=[]
        def stop_recording(self):
            assert self.recording, "not recording"
            if self.fail: raise OSError("encoder failed")
            if self.recorded_frames:
                (Path(self.video_folder)/(self._video_name+".mp4")).write_bytes(b"modern-mp4")
            self.recording=False; self._video_name=None; self.recorded_frames=[]
        def close(self):
            self.closes += 1
            if self.recording: self.stop_recording()

    class ImageEncoder:
        def __init__(self, output_path): self.output_path=str(output_path)
        def close(self): Path(self.output_path).write_bytes(b"legacy-mp4")

    modern = name == "gymnasium" and int(version.split(".")[0]) >= 1
    if not modern:
        class RecordVideo: pass
    wrappers.RecordVideo=RecordVideo
    module.VideoRecorder=VideoRecorder; module.ImageEncoder=ImageEncoder
    package.wrappers=wrappers
    return {name:package, name+".wrappers":wrappers, module.__name__:module}
