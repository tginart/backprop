"""Verify video encoding with real MoviePy and the pinned W&B Video wrapper.

Architecture: generate tiny CPU-only frames -> encode both implementations ->
decode with the installed ffmpeg -> compare pixels, dimensions and frame rate.
The reference class body is unchanged; only W&B's storage and spinner boundaries
are supplied locally. Native logs/artifacts then roundtrip the encoded bytes.
Main callers: unittest discovery in an existing media-capable environment.
No packages, codecs, GPU jobs or background services are installed or started.
"""
from __future__ import annotations

import ast
from io import BytesIO
import gc
import hashlib
import importlib
import importlib.util
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import backprop
from backprop.transport import MemorySink

SOURCE = Path(__file__).resolve().parents[1] / ".reference/wandb/source/wandb/sdk/data_types/video.py"
SOURCE_SHA256 = "364916680428208f4299d21f096ade0c175e2f70bee8b26c8899818310491dc4"
HAS_RUNTIME = all(importlib.util.find_spec(name) for name in ("numpy", "moviepy", "imageio_ffmpeg"))


def reference_video(directory):
    """Load the exact pinned class with a local temporary-file boundary.

    Main callers: differential video tests. This does not emulate the complete
    W&B SDK: real MoviePy/imageio/NumPy execute all encoding and pixel operations.
    """
    source = SOURCE.read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise AssertionError("Video source differs from pinned W&B 0.30.0")
    import numpy as np
    class ReferenceMedia:
        def __init__(self, caption=None):
            self._caption = caption

        def _set_file(self, path, is_tmp=False):
            self._path = path
            # Pinned Media._set_file reads bytes immediately, so absent codec
            # output must fail at the same boundary, not become a valid object.
            self._sha256 = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    nodes = [node for node in ast.parse(source).body if
             isinstance(node, ast.ImportFrom) and node.module == "__future__" or
             isinstance(node, ast.ClassDef) and node.name == "Video" or
             isinstance(node, ast.FunctionDef) and node.name == "write_gif_with_image_io"]
    warnings = []
    namespace = {"BatchableMedia": ReferenceMedia, "BytesIO": BytesIO,
        "os": os, "pathlib": importlib.import_module("pathlib"), "logging": logging,
        "MEDIA_TMP": SimpleNamespace(name=str(directory)),
        "runid": SimpleNamespace(generate_id=lambda: uuid.uuid4().hex),
        "wandb": SimpleNamespace(termwarn=warnings.append),
        "util": SimpleNamespace(get_module=lambda name, **kwargs: importlib.import_module(name),
                                is_numpy_array=lambda value: isinstance(value, np.ndarray)),
        "_should_print_spinner": lambda: False}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["Video"], warnings


def decoded(path):
    """Read actual codec output, with no mock or browser decoder assumptions."""
    import imageio_ffmpeg
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    try:
        metadata = next(reader)
        frames = list(reader)
    finally:
        reader.close()
    return metadata, frames


@unittest.skipUnless(HAS_RUNTIME, "Genuine NumPy/MoviePy/imageio-ffmpeg runtime is unavailable")
class VideoRuntimeContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="backprop-video-test-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def frames(self, batch=1):
        import numpy as np
        result = np.zeros((batch, 4, 3, 32, 32), dtype=np.uint8)
        for index in range(batch):
            for frame in range(4):
                result[index, frame, (index + frame) % 3] = 200
        return result[0] if batch == 1 else result

    @unittest.skipUnless(SOURCE.is_file(), "Pinned W&B source is required for encoder comparisons")
    def test_gif_mp4_webm_pixels_and_metadata_match_pinned_encoder(self):
        import numpy as np
        reference, _warnings = reference_video(self.root)
        for format in ("gif", "mp4", "webm"):
            for batch in (1, 3):
                with self.subTest(format=format, batch=batch):
                    frames = self.frames(batch)
                    before = frames.copy()
                    expected = reference(frames, format=format, fps=4, caption="clip")
                    actual = backprop.Video(frames, format=format, fps=4, caption="clip")
                    path = self.root / ("actual." + format)
                    path.write_bytes(actual.data)
                    old_meta, old_frames = decoded(expected._path)
                    meta, output_frames = decoded(path)
                    self.assertEqual(meta["size"], old_meta["size"])
                    self.assertAlmostEqual(meta["fps"], old_meta["fps"])
                    self.assertEqual(len(output_frames), len(old_frames))
                    self.assertEqual(output_frames, old_frames)
                    self.assertEqual(actual.metadata["width"], expected._width)
                    self.assertEqual(actual.metadata["height"], expected._height)
                    np.testing.assert_array_equal(frames, before)

    @unittest.skipUnless(SOURCE.is_file(), "Pinned W&B source is required for encode comparisons")
    def test_direct_encode_fps_preserves_pinned_validation_and_defaults(self):
        reference, _warnings = reference_video(self.root)
        for format in ("gif", "mp4"):
            for fps in (0, None, -1, "4", 1.5):
                with self.subTest(format=format, fps=fps):
                    outcomes = []
                    for factory in (reference, backprop.Video):
                        unraisable = []
                        # Negative GIF duration also fails in imageio's object
                        # destructor. Capture and compare those actual failures;
                        # this test must not mistake them for clean encoding.
                        with patch("sys.unraisablehook", lambda item: unraisable.append(
                                (item.exc_type.__name__, str(item.exc_value)))):
                            value = factory(self.frames(), format=format, fps=4)
                            try:
                                value.encode(fps=fps)
                            except OSError as error:
                                # Native temporary filenames intentionally differ.
                                outcome = ("error", type(error), error.errno)
                            except Exception as error:
                                outcome = ("error", type(error), str(error))
                            else:
                                if isinstance(value, backprop.Video):
                                    path = self.root / ("reencoded." + format)
                                    path.write_bytes(value.data)
                                else:
                                    path = value._path
                                metadata, frames = decoded(path)
                                outcome = ("result", metadata["fps"], metadata["size"], frames)
                            gc.collect()
                        outcomes.append((outcome, unraisable))
                    self.assertEqual(outcomes[0], outcomes[1])

    @unittest.skipUnless(SOURCE.is_file(), "Pinned W&B source is required for encoder comparisons")
    def test_ogg_matches_actual_pinned_codec_result_or_failure(self):
        reference, _warnings = reference_video(self.root)
        for batch in (1, 3):
            with self.subTest(batch=batch):
                try:
                    expected = reference(self.frames(batch), format="ogg", fps=4)
                except Exception as error:
                    # MoviePy 2.2.1 maps .ogg to an audio codec; its writer can
                    # finish without a file. Both SDKs must propagate failure.
                    with self.assertRaises(type(error)):
                        backprop.Video(self.frames(batch), format="ogg", fps=4)
                else:
                    actual = backprop.Video(self.frames(batch), format="ogg", fps=4)
                    path = self.root / "actual.ogg"
                    path.write_bytes(actual.data)
                    self.assertEqual(decoded(path), decoded(expected._path))

    @unittest.skipUnless(SOURCE.is_file(), "Pinned W&B source is required for encoder comparisons")
    def test_false_fps_defaults_and_uint8_conversion_warning_match_pinned(self):
        import numpy as np
        reference, _warnings = reference_video(self.root)
        for fps in (None, 0, False):
            with self.subTest(fps=fps):
                expected = reference(self.frames(), format="mp4", fps=fps)
                actual = backprop.Video(self.frames(), format="mp4", fps=fps)
                path = self.root / "defaults.mp4"
                path.write_bytes(actual.data)
                self.assertEqual(decoded(path)[1], decoded(expected._path)[1])
                self.assertEqual(actual.metadata["fps"], 4)
        for factory in (reference, backprop.Video):
            with self.assertLogs(level="WARNING") as logged:
                factory(self.frames().astype(np.float32), format="mp4")
            self.assertEqual(logged.output, ["WARNING:root:Converting video data to uint8"])

    @unittest.skipUnless(SOURCE.is_file(), "Pinned W&B source is required for constructor comparisons")
    def test_encoded_input_warning_and_format_validation_match_pinned(self):
        reference, reference_warnings = reference_video(self.root)
        path = self.root / "input.mp4"
        path.write_bytes(b"encoded bytes")
        for source in (str(path), path, BytesIO(b"encoded bytes")):
            for fps in (None, 0, 4):
                with self.subTest(source=type(source).__name__, fps=fps):
                    reference_warnings.clear()
                    if hasattr(source, "seek"):
                        source.seek(0)
                    reference(source, format="mp4", fps=fps)
                    if hasattr(source, "seek"):
                        source.seek(0)
                    with patch("backprop.termwarn") as warn:
                        backprop.Video(source, format="mp4", fps=fps)
                    self.assertEqual([call.args[0] for call in warn.call_args_list], reference_warnings)
        for source in (BytesIO(b"encoded"), self.frames()):
            for format in (None, "avi", "MP4"):
                errors = []
                for factory in (reference, backprop.Video):
                    try:
                        factory(source, format=format)
                    except Exception as error:
                        errors.append((type(error), str(error)))
                self.assertEqual(len(errors), 2)
                self.assertEqual(errors[0], errors[1])

    def test_reencode_changes_fps_and_logs_and_artifacts_keep_exact_bytes(self):
        value = backprop.Video(self.frames(), format="mp4", fps=4, caption="sample")
        value.encode(fps=8)
        path = self.root / "encoded.mp4"
        path.write_bytes(value.data)
        metadata, frames = decoded(path)
        self.assertAlmostEqual(metadata["fps"], 8)
        self.assertEqual(len(frames), 4)
        sink = MemorySink()
        with backprop.Run(project="video-runtime", entity="tests", id="video", dir=self.root,
                          _sink=sink, _deployment={}, settings={"console": "off", "silent": True,
                          "disable_git": True, "flush_interval": 3600}) as run:
            run.log({"clip": value})
            artifact = backprop.Artifact("encoded-video", "media")
            artifact.add(value, "clip")
            run.log_artifact(artifact)
        api = backprop.Api._from_store(sink, entity="tests", project="video-runtime")
        row = list(api.run("video").scan_history())[0]
        self.assertEqual(sink.download_file(row["clip"]), value.data)
        restored = api.artifact(artifact.qualified_name).get("clip")
        self.assertEqual(restored.data, value.data)
        self.assertEqual(restored.caption, "sample")


if __name__ == "__main__":
    unittest.main()
