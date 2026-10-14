"""Evaluation tables stored through existing Foundry artifacts and journals.

Architecture: retain raw Table cells -> validate/normalize at publication ->
commit one immutable evaluation artifact -> expose its stable identity. Binding
establishes a run owner without creating a media file. The shared Encoder stages
publication atomically with history; explicit to_json publishes the same artifact
without adding a history step. Main callers: user logging and artifact codecs.
Why this exists: W&B delegates evaluation storage to Weave. Backprop retains the
portable lifecycle while using its existing Foundry storage and table viewer.
"""
from __future__ import annotations
import copy
import sys
from .tables import Table
from .errors import UsageError
from . import evaluation


class EvalTable(Table):
    """An immutable evaluation with explicit input, output and score columns."""
    _log_type = "eval-table"

    def __init__(self, columns=None, data=None, rows=None, dataframe=None, dtype=None,
                 optional=True, allow_mixed_types=False, log_mode="IMMUTABLE", *,
                 input_columns=None, output_columns=None, score_columns=None, unsupported_media_mode="stub"):
        if log_mode != "IMMUTABLE":
            raise UsageError("EvalTable currently only supports log_mode='IMMUTABLE'.")
        self._input_columns = list(input_columns or [])
        self._output_columns = list(output_columns or [])
        self._score_columns = list(score_columns or [])
        self._immutable_evaluate_call_id = None
        self._immutable_logged_json = None
        self._evaluation_record = None
        self._run_log_key = None
        self._native_evaluation_snapshot = False
        if unsupported_media_mode not in ("stub", "raise"):
            raise ValueError("unsupported_media_mode must be 'stub' or 'raise'")
        self._unsupported_media_mode = unsupported_media_mode
        if columns is None and dataframe is None and (self._input_columns or self._output_columns or self._score_columns):
            columns = [*self._input_columns, *self._output_columns, *self._score_columns]
        super().__init__(columns, data, rows, dataframe, dtype, optional, allow_mixed_types, log_mode)

    def add_data(self, *data):
        if len(data) == len(self.columns):
            for column, value in zip(self.columns, data):
                evaluation.validate_value(value, column, self._unsupported_media_mode)
        return super().add_data(*data)

    def add_column(self, name, data, optional=False):
        np = sys.modules.get('numpy')
        if isinstance(data, list) or np is not None and isinstance(data, np.ndarray):
            for value in data:
                evaluation.validate_value(value, name, self._unsupported_media_mode)
        return super().add_column(name, data, optional)

    def bind_to_run(self, run, key, step, id_=None, ignore_copy_err=None):
        """Establish the owner without creating a Table media file.

        Main callers: portable media binding and explicit serialization. step,
        id_ and ignore_copy_err have no file-copy effect, as in the reference.
        Foundry's durable queue additionally supports offline evaluations.
        """
        # Access the same owner attributes as upstream before mutating state.
        run.offline, run.entity, run.project
        self._run, self._run_log_key = run, str(key)

    def has_been_logged(self):
        return self._immutable_evaluate_call_id is not None

    def to_json(self, run_or_artifact):
        """Publish once and return the stable evaluation descriptor.

        Main callers: the public Python API. Run.log uses Encoder directly so
        its descriptor and artifact manifest share one durable event. Explicit
        serialization uses the existing artifact event under the same run lock.
        Artifact serialization is a retained Backprop extension to W&B.
        """
        from .artifacts import Artifact
        from .run import Run
        if isinstance(run_or_artifact, Artifact):
            snapshot = evaluation.capture(self)
            return {**Table.to_json(snapshot, run_or_artifact), **snapshot.evaluation_metadata()}
        if not isinstance(run_or_artifact, Run):
            raise TypeError("EvalTable can only be serialized for a Run or Artifact")
        run = run_or_artifact
        if self._run_log_key is None:
            raise UsageError("EvalTable must be logged with run.log().")
        if self._run is not run:
            raise UsageError("EvalTable cannot be serialized for a different run than it was bound to.")
        with run._lock:
            if self._immutable_logged_json is not None:
                from .evaluation_publication import flush_published
                flush_published(self)
                self._warn_immutable_already_logged()
                return dict(self._immutable_logged_json)
            if run.disabled or run._closed:
                from .evaluation_publication import publish
                return publish(self, run)
            run._check()
            encoder = run._encoder()
            encoder.key = self._run_log_key
            encoder.encode(self)
            if len(encoder.artifacts) != 1:
                raise RuntimeError('An evaluation must publish exactly one artifact manifest')
            run._emit('artifact', encoder.artifacts[0])
            encoder.commit()
            return dict(self._immutable_logged_json)

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        return super().from_json(json_obj, source_artifact)

    def _warn_immutable_already_logged(self):
        import backprop
        backprop.termwarn("EvalTable with log_mode='IMMUTABLE' has already been logged. Subsequent run.log() calls have no effect.", repeat=False)

    @property
    def evaluation_roles(self):
        return self.evaluation_metadata()['evaluation_roles']

    def evaluation_metadata(self):
        """Return roles and score summaries from the published snapshot.

        Main callers: table encoders and inspection tools. Before publication a
        candidate is computed without modifying raw cells or binding the table.
        """
        frozen = getattr(self, '_evaluation_frozen_metadata', None)
        if frozen is None and self._evaluation_record is not None:
            frozen = {key: self._evaluation_record[key] for key in
                      ('evaluation_roles', 'evaluation_scores', 'evaluation_row_identity')}
        if frozen is None:
            frozen = evaluation.capture(self)._evaluation_frozen_metadata
        return copy.deepcopy(frozen)
