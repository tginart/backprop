"""Source-backed external Weave Audio boundary for the pinned EvalTable probe.

Architecture: genuine W&B audio adapter -> this file/format object -> genuine
EvalTable normalization and EvaluationLogger service fixture. No W&B method is
replaced. The checks and base64 rule follow Weave v0.52.41:
https://github.com/wandb/weave/blob/v0.52.41/weave/type_handlers/Audio/audio.py
Source SHA256: c55d62f344a7990e0b9317d318eadab0490915dfbb43d56511c08446ac760a63.
Main caller: eval_table_reference_probe.py. No Weave installation is needed.
"""
import base64
import binascii
from pathlib import Path


class Audio:
    def __init__(self, data, format):
        if not data:
            raise ValueError('Audio data cannot be empty')
        try:
            data = base64.b64decode(data, validate=True)
        except binascii.Error:
            pass
        self.data, self.format = data, format

    @classmethod
    def from_path(cls, path):
        path = Path(path)
        if not path.exists() or path.suffix.lower() not in {'.mp3', '.wav'}:
            raise ValueError('Expected an existing WAV or MP3 file')
        return cls(path.read_bytes(), path.suffix.lower()[1:])
