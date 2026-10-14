"""Video preparation matches pinned source; optional codecs stay explicit.

Architecture: execute the pinned preparation method with genuine NumPy ->
compare shapes/dtypes/pixels/errors -> test dependency, FPS forwarding and
resource cleanup boundaries with focused fixtures. Full codec/file comparison
lives in test_sdk_video_runtime.py and requires the existing MoviePy runtime.
Main callers: unittest discovery; no optional packages are installed here.
"""
import ast
import hashlib
import importlib
import importlib.util
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop
from backprop import video_encoding as encoding

SOURCE = Path(__file__).resolve().parents[1] / ".reference/wandb/source/wandb/sdk/data_types/video.py"
SOURCE_SHA256 = "364916680428208f4299d21f096ade0c175e2f70bee8b26c8899818310491dc4"
HAS_NUMPY = importlib.util.find_spec("numpy") is not None
HAS_PACKAGING = importlib.util.find_spec("packaging") is not None


def pinned_prepare():
    """Run the exact pinned method body, supplying only its import boundary."""
    source = SOURCE.read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise AssertionError("Unexpected pinned W&B Video source")
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Video")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_prepare_video")
    namespace = {"util": SimpleNamespace(get_module=lambda name, **kwargs: importlib.import_module(name)),
                 "logging": logging}
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, method], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return lambda frames: namespace["_prepare_video"](None, frames)


class VideoEncodingBoundary(unittest.TestCase):
    def test_constructor_defaults_but_direct_encode_forwards_false_fps_unchanged(self):
        raw = object()
        eager = SimpleNamespace(numpy=lambda: raw)
        def encoded(array, format, fps):
            return b"encoded", {"width": 4, "height": 3, "channels": 3, "fps": fps}
        with patch.object(encoding, "encode_video", side_effect=encoded) as codec:
            for fps in (None, 0, False, 4, -1, "invalid"):
                value = backprop.Video(eager, format="mp4", fps=fps, caption="clip")
                self.assertIs(value._raw_data, raw)
                self.assertEqual(codec.call_args.args, (raw, "mp4", fps or 4))
                value.encode(fps=fps)
                self.assertEqual(codec.call_args.args, (raw, "mp4", fps))
                self.assertEqual(value.metadata["fps"], fps)
                self.assertEqual(value.caption, "clip")
        before = value.data, dict(value.metadata)
        with patch.object(encoding, "encode_video", side_effect=ZeroDivisionError("codec FPS failure")):
            with self.assertRaises(ZeroDivisionError):
                value.encode(fps=0)
        self.assertEqual((value.data, value.metadata), before)

    def test_unrelated_array_like_inputs_are_not_silently_coerced(self):
        with patch.object(encoding, "encode_video", side_effect=AssertionError("invalid raw input reached codec")):
            for value in ([1, 2], 7, object(), SimpleNamespace(tolist=lambda: [])):
                with self.subTest(type=type(value).__name__), self.assertRaisesRegex(ValueError, "file path or numpy like"):
                    backprop.Video(value, format="gif")

    def test_missing_and_broken_optional_dependencies_raise_exact_public_errors(self):
        for name, message in encoding._REQUIRED.items():
            for error in (ModuleNotFoundError(name), RuntimeError("broken optional binary")):
                with self.subTest(module=name, error=type(error).__name__), patch.object(
                        encoding.importlib, "import_module", side_effect=error):
                    with self.assertRaises(backprop.Error) as raised:
                        encoding._require_module(name)
                    self.assertEqual(str(raised.exception), message)
        with patch.object(encoding, "_require_module", side_effect=backprop.Error("MoviePy unavailable")) as require:
            with self.assertRaisesRegex(backprop.Error, "MoviePy unavailable"):
                encoding.encode_video(None, "gif", 4)
            require.assert_called_once_with("moviepy.video.io.ImageSequenceClip")

    @unittest.skipUnless(HAS_PACKAGING, "packaging is an optional video dependency")
    def test_gif_writer_uses_pinned_options_and_closes_after_failure(self):
        class Writer:
            def __init__(self, fail=False):
                self.frames, self.closed, self.fail = [], False, fail
            def append_data(self, frame):
                self.frames.append(frame)
                if self.fail:
                    raise OSError("frame write failed")
            def close(self):
                self.closed = True
        for fail in (False, True):
            writer = Writer(fail)
            calls = []
            def save(filename, **options):
                calls.append((filename, options))
                return writer
            def frames(**options):
                self.assertEqual(options, {"fps": None, "dtype": "uint8"})
                yield "frame1"
                yield "frame2"
            imageio = SimpleNamespace(__version__="2.36.0", save=save)
            clip = SimpleNamespace(fps=5, iter_frames=frames)
            with patch.object(encoding, "_require_module", return_value=imageio):
                if fail:
                    with self.assertRaisesRegex(OSError, "frame write failed"):
                        encoding.write_gif_with_image_io(clip, "output.gif")
                else:
                    encoding.write_gif_with_image_io(clip, "output.gif")
            self.assertTrue(writer.closed)
            self.assertEqual(calls, [("output.gif", {"quantizer": 0, "palettesize": 256, "loop": 0, "duration": 200})])
            self.assertEqual(writer.frames, ["frame1"] if fail else ["frame1", "frame2"])
        with patch.object(encoding, "_require_module", return_value=SimpleNamespace(__version__="2.28.0")):
            with self.assertRaisesRegex(ValueError, "2.28.1 or higher is required"):
                encoding.write_gif_with_image_io(clip, "output.gif")

    def test_codec_failure_closes_clip_and_removes_owned_temporary_files(self):
        class Frames:
            shape = (2, 3, 4, 3)
            def __iter__(self):
                return iter(["frame1", "frame2"])
        paths, closed, parameters = [], [], []
        def factory(frames, fps):
            parameters.append((frames, fps))
            def write(path, **options):
                paths.append(Path(path))
                raise OSError("codec failed")
            return SimpleNamespace(write_videofile=write, close=lambda: closed.append(True))
        with patch.object(encoding, "_require_module", return_value=SimpleNamespace(ImageSequenceClip=factory)), patch.object(
                encoding, "prepare_video", return_value=Frames()):
            with self.assertRaisesRegex(OSError, "codec failed"):
                encoding.encode_video(object(), "mp4", 0)
        self.assertEqual(parameters, [(["frame1", "frame2"], 0)])
        self.assertEqual(closed, [True])
        self.assertFalse(paths[0].parent.exists())


@unittest.skipUnless(HAS_NUMPY and SOURCE.is_file(), "NumPy and the pinned source snapshot are required")
class VideoPreparationReference(unittest.TestCase):
    def test_numpy_preparation_pixels_dtypes_errors_and_input_preservation_match_pinned(self):
        import numpy as np
        reference = pinned_prepare()
        arrays = [np.arange(2 * 3 * 2 * 2, dtype=np.uint8).reshape(2, 3, 2, 2)]
        for batch in (1, 2, 3, 5, 8):
            for dtype in (np.uint8, np.float32):
                arrays.append(np.arange(batch * 2 * 3 * 2 * 2).reshape(batch, 2, 3, 2, 2).astype(dtype))
        arrays += [np.zeros(shape, dtype=np.uint8) for shape in
                   ((2, 3), (1, 2, 3), (1, 1, 1, 1, 1, 1), (0, 2, 3, 2, 2),
                    (1, 0, 3, 2, 2), (1, 2, 2, 2, 2), (1, 2, 3, 0, 2))]
        for array in arrays:
            with self.subTest(shape=array.shape, dtype=str(array.dtype)):
                before = array.copy()
                outcomes = []
                for prepare in (reference, encoding.prepare_video):
                    with patch.object(logging, "warning") as warning:
                        try:
                            result = prepare(array)
                            outcomes.append((None, result, warning.call_args_list))
                        except Exception as error:
                            outcomes.append((type(error), str(error), warning.call_args_list))
                expected, actual = outcomes
                self.assertEqual(expected[0], actual[0])
                self.assertEqual(expected[2], actual[2])
                if expected[0] is None:
                    self.assertEqual(expected[1].dtype, actual[1].dtype)
                    np.testing.assert_array_equal(expected[1], actual[1])
                else:
                    self.assertEqual(expected[1], actual[1])
                np.testing.assert_array_equal(array, before)


if __name__ == "__main__":
    unittest.main()
