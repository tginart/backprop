"""Own PyTorch observation hooks for one run, including repeated registrations.

Architecture: validate models -> install one registration transaction at a time
-> retain every owned hook -> remove those hooks on unwatch/finish. Gradient
names are unique within a run while live; distinct indices can observe the same
model more than once. Graph capture has one active registration per model.
Main callers: logging Run.watch/unwatch, with the historical telemetry import
kept as an alias. Run ownership avoids the pinned SDK's process-global sink.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import importlib

from .torch_histogram import HistogramRecorder
from .errors import Error


@dataclass
class _ObservedModel:
    model: object
    handles: list = field(default_factory=list)
    gradients: dict = field(default_factory=dict)


def _live(handle):
    """Check whether a caller has already removed a PyTorch hook.

    Main caller: duplicate gradient-name validation. Removed handles may remain
    in our ownership list until unwatch; they must not block a new registration.
    """
    hooks = handle.hooks_dict_ref()
    return hooks is not None and handle.id in hooks


class ModelWatcher:
    """Keep each run's registrations independent of other runs on the model."""

    def __init__(self, run):
        self.run = run
        self.models = {}
        self.next_index = 0
        self.graphs = {}
        self._gradient_handles = {}
        self._recorder = HistogramRecorder(run)

    def watch(self, models, criterion=None, log="gradients", log_freq=1000, idx=None, log_graph=False):
        """Install histogram and optional graph hooks for each validated model.

        Main caller: Run.watch, which discards this private graph-handle return.
        Distinct indices permit repeated histogram observations. A failed new
        registration removes only its new hooks, preserving older observations.
        """
        if log not in {None, "gradients", "parameters", "all"}:
            raise ValueError("log must be one of 'gradients', 'parameters', 'all', or None")
        try:
            torch = importlib.import_module("torch")
        except ModuleNotFoundError as error:
            if error.name != "torch":
                raise
            raise Error("wandb.watch only works with pytorch, couldn't import torch.") from error
        models = models if isinstance(models, (list, tuple)) else (models,)
        for model in models:
            if not isinstance(model, torch.nn.Module):
                raise TypeError(f"Expected a pytorch model (torch.nn.Module). Received {type(model)}")
        graphs = []
        initial_index = self.next_index if idx is None else idx
        for offset, model in enumerate(models):
            number = initial_index + offset
            self.next_index += 1
            graph = self._install(model, criterion, log, log_freq, number, log_graph)
            if graph is not None:
                graphs.append(graph)
        return graphs

    def _install(self, model, criterion, log, frequency, index, log_graph):
        """Publish ownership only after a complete registration succeeds.

        Main caller: watch. Existing observations remain live when duplicate
        gradient names or graph registration errors reject this attempt.
        """
        from . import termwarn
        pending = _ObservedModel(model)
        prefix = f"graph_{index}" if index > 0 else ""
        graph = None
        try:
            if log in {"parameters", "all"}:
                try:
                    pending.handles.append(self._parameter_hook(model, prefix, frequency))
                except RuntimeError as error:
                    termwarn(f"Trying to register forward_hook failed ({error}) - skipping parameter tracking.")
            if log in {"gradients", "all"}:
                for name, parameter in model.named_parameters():
                    if parameter.requires_grad:
                        key = f"gradients/{prefix}{name}"
                        handle = self._gradient_hook(parameter, key, frequency)
                        pending.handles.append(handle)
                        pending.gradients[key] = handle
            if log_graph:
                if id(model) in self.graphs:
                    raise ValueError("You can only call `wandb.watch` once per model.  Pass a new instance of the model if you need to call wandb.watch again in your code.")
                from .torch_graph import TorchGraph
                graph = TorchGraph(self.run, model, criterion, index, pending.handles)
        except BaseException:
            self._remove(pending)
            raise
        if log is not None or log_graph:
            owner = self.models.setdefault(id(model), _ObservedModel(model))
            owner.handles.extend(pending.handles)
            owner.gradients.update(pending.gradients)
        if graph is not None:
            self.graphs[id(model)] = graph
        return graph

    def _parameter_hook(self, model, prefix, frequency):
        """Count forwards for this registration, then record detached values.

        Main caller: _install. Every registration owns its frequency counter,
        even when the same model is watched with several prefixes.
        """
        count = 0
        def forward(module, inputs, output):
            nonlocal count
            count += 1
            if count < frequency:
                return
            count = 0
            for name, parameter in module.named_parameters():
                self._recorder.record(parameter, f"parameters/{prefix}{name}")
        return model.register_forward_hook(forward)

    def _gradient_hook(self, parameter, name, frequency):
        """Reject live name collisions and count backward calls independently.

        Main caller: _install. Returning the original gradient preserves training
        behavior; retained handles can be removed without touching other runs.
        """
        previous = self._gradient_handles.get(name)
        if previous is not None and _live(previous):
            raise ValueError(f'A hook has already been set under name "{name}"')
        count = 0
        def gradient(value):
            nonlocal count
            count += 1
            if count < frequency:
                return value
            count = 0
            self._recorder.record(value, name)
            return value
        handle = parameter.register_hook(gradient)
        self._gradient_handles[name] = handle
        return handle

    def _remove(self, owner):
        """Remove owned handles and only their matching gradient-name entries.

        Main callers: rollback and unwatch. A removed old handle must not erase
        a newer registration that legitimately reused its name.
        """
        for handle in owner.handles:
            handle.remove()
        for name, handle in owner.gradients.items():
            if self._gradient_handles.get(name) is handle:
                del self._gradient_handles[name]

    def unwatch(self, models=None):
        """Remove all observations or selected models, warning for unknown ones.

        Main callers: Run.unwatch and finish. The pinned public contract treats
        a false-valued selection, including an empty list, as all models.
        """
        from . import termwarn
        if not models:
            selected = [owner.model for owner in self.models.values()]
        else:
            selected = models if isinstance(models, (list, tuple)) else (models,)
        for model in selected:
            owner = self.models.pop(id(model), None)
            if owner is None:
                termwarn(f"{model} model has not been watched")
                continue
            self.graphs.pop(id(model), None)
            self._remove(owner)
