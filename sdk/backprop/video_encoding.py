"""Pinned W&B video preparation and optional MoviePy/imageio encoding.

Architecture: select a NumPy array or eager tensor -> arrange channel-first
frames into the pinned batch grid -> let MoviePy encode the sequence -> return
encoded bytes and dimensions. GIF uses W&B's imageio writer rather than a second
Pillow implementation. Temporary files and codec writers are closed on failure.
Main callers: media.Video construction, _prepare_video and direct encode().
Why this exists: constructor FPS defaults differ from direct encode arguments;
calling the constructor to re-encode silently changes explicit false values.
Algorithms: W&B v0.30.0 sdk/data_types/video.py (MIT).
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
import tempfile


_REQUIRED = {
    "numpy": 'wandb.Video requires numpy when passing raw data. To get it, run "pip install numpy".',
    "moviepy.video.io.ImageSequenceClip": 'wandb.Video requires moviepy when passing raw data. Install with "pip install wandb[media]"',
    "imageio": 'wandb.Video requires imageio when passing raw data. Install with "pip install wandb[media]"',
}


def _require_module(name):
    """Surface missing or broken optional imports as the pinned public Error."""
    from .errors import Error
    try:
        return importlib.import_module(name)
    except Exception as error:
        raise Error(_REQUIRED[name]) from error


def raw_video(value):
    """Accept documented NumPy arrays and eager objects exposing numpy().

    Main callers: Video.__init__. Preserve the original array for a later
    explicit re-encode, as upstream does. Preparation does not modify it. Lists
    and unrelated array-like objects do not silently become accepted inputs.
    """
    if hasattr(value, "numpy"):
        return value.numpy()
    try:
        np = importlib.import_module("numpy")
    except Exception:
        np = None
    if np is not None and isinstance(value, np.ndarray):
        return value
    raise ValueError("wandb.Video accepts a file path or numpy like data as input")


def prepare_video(video):
    """Arrange raw frames with the exact pinned padding, dtype and shape rules.

    Main callers: Video._prepare_video and encode_video. The upstream NumPy
    operations define empty, oversized-dimensional and channel-shape errors.
    Preserve its default float padding, including the resulting dtype change
    for non-power-of-two batch sizes, before MoviePy consumes those frames.
    """
    np = _require_module("numpy")
    if video.ndim < 4:
        raise ValueError("Video must be at least 4 dimensions: time, channels, height, width")
    if video.ndim == 4:
        video = video.reshape(1, *video.shape)
    batch, frames, channels, height, width = video.shape
    if video.dtype != np.uint8:
        logging.warning("Converting video data to uint8")
        video = video.astype(np.uint8)
    if not (video.shape[0] != 0 and ((video.shape[0] & (video.shape[0] - 1)) == 0)):
        addition = int(2 ** video.shape[0].bit_length() - video.shape[0])
        video = np.concatenate((video, np.zeros(shape=(addition, frames, channels, height, width))), axis=0)
    rows = 2 ** ((batch.bit_length() - 1) // 2)
    columns = video.shape[0] // rows
    video = video.reshape(rows, columns, frames, channels, height, width)
    video = np.transpose(video, axes=(2, 0, 4, 1, 5, 3))
    return video.reshape(frames, rows * height, columns * width, channels)


def write_gif_with_image_io(clip, filename, fps=None):
    """Use the pinned imageio GIF settings and close its writer deterministically.

    Main callers: encode_video. Clip FPS sets duration; the optional function
    argument independently controls iter_frames, matching W&B's helper.
    """
    from packaging.version import parse
    imageio = _require_module("imageio")
    if parse(imageio.__version__) < parse("2.28.1"):
        raise ValueError("imageio version 2.28.1 or higher is required to encode gifs. "
                         "Please upgrade imageio with `pip install imageio>=2.28.1`")
    writer = imageio.save(filename, quantizer=0, palettesize=256, loop=0, duration=1000 / clip.fps)
    try:
        for frame in clip.iter_frames(fps=fps, dtype="uint8"):
            writer.append_data(frame)
    finally:
        writer.close()


def encode_video(raw, format, fps):
    """Encode one raw array without normalizing the caller's FPS argument.

    Main callers: Video constructor (after fps or 4) and Video.encode (unchanged
    fps). Load MoviePy before preparation, including GIF. Codec or invalid-FPS
    errors propagate; the caller swaps its previous bytes only on success.
    """
    module = _require_module("moviepy.video.io.ImageSequenceClip")
    frames = prepare_video(raw)
    _, height, width, channels = frames.shape
    clip = module.ImageSequenceClip(list(frames), fps=fps)
    try:
        with tempfile.TemporaryDirectory(prefix="backprop-video-") as directory:
            path = Path(directory) / ("video." + format)
            if format == "gif":
                write_gif_with_image_io(clip, str(path))
            else:
                clip.write_videofile(str(path), logger=None)
            data = path.read_bytes()
    finally:
        clip.close()
    return data, {"width": int(width), "height": int(height), "channels": int(channels), "fps": fps}
