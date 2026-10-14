"""Capture observed PyTorch modules without tracing or a hosted graph service.

Architecture: install forward hooks -> snapshot each observed module once ->
write graph-file summaries -> remove owned hooks on unwatch/finish. Container
children are visited recursively; parameter identities point back to their node.
Unexecuted branches stay explicitly incomplete, never invented as graph edges.
Main callers: ModelWatcher.watch. Optional PyTorch is imported only on use.
The shape and node algorithms follow W&B 0.30.0 wandb_torch.py (MIT).
"""
from __future__ import annotations

import math
import threading

from .graph import Graph, Node


def nested_shape(value, seen=None):
    """Read tensor dimensions without retaining tensors in the serialized graph.

    Main callers: module forward hooks. Cycles use the pinned zero sentinel.
    """
    seen = set() if seen is None else seen
    if hasattr(value, "size"):
        return list(value.size())
    if hasattr(value, "get_shape"):
        return value.get_shape().as_list()
    if hasattr(value, "shape"):
        return value.shape
    seen.add(id(value))
    try:
        return [nested_shape(item, seen) if id(item) not in seen else 0 for item in list(value)]
    except TypeError:
        return []


class TorchGraph(Graph):
    """A live graph handle whose snapshots preserve missing module observations."""

    _registry_exclude = True

    def __init__(self, run, model, criterion, graph_idx, handles):
        super().__init__("torch")
        self._owner_run = run
        self._graph_idx = graph_idx
        self._pending = {}
        self._capture_lock = threading.RLock()
        self.criterion = criterion
        self.criterion_passed = criterion is not None
        import torch
        containers = tuple(getattr(torch.nn, name) for name in
                           ("Container", "Sequential", "ModuleList", "ModuleDict")
                           if hasattr(torch.nn, name))

        def collect(module, prefix=""):
            for name, child in module.named_children():
                name = f"{prefix}.{name}" if prefix else name
                if isinstance(child, containers):
                    yield from collect(child, name)
                else:
                    yield name, child
        modules = list(collect(model))
        # A standalone layer is itself useful topology; upstream only hooks
        # children and leaves that graph permanently empty.
        if not modules and not list(model.children()):
            modules = [(type(model).__name__, model)]
        self._pending = {id(module): name for name, module in modules}
        for name, module in modules:
            handles.append(module.register_forward_hook(self._forward(name, module)))
        self._publish()

    def _forward(self, name, source_module):
        """Observe a registered logical layer even when PyTorch clones it.

        Main callers: initialization. DataParallel replicas copy forward hooks
        but change module identities and expose tensors as former parameters.
        The original module supplies identity/parameter metadata; the first
        observed replica supplies output shape. A lock deduplicates concurrent
        replica callbacks without counting an unexecuted layer as complete. The
        run lock makes observation and publication atomic relative to finish.
        """
        identity = id(source_module)
        def observed(module, inputs, output):
            with self._capture_lock, self._owner_run._lock:
                if identity not in self._pending or self._owner_run._closed:
                    return
                outputs = output if isinstance(output, tuple) else (output,)
                parameters = [(key, list(parameter.size())) for key, parameter in source_module.named_parameters()]
                node = Node(id=identity, name=name, class_name=str(source_module),
                            output_shape=nested_shape(outputs), parameters=parameters,
                            num_parameters=[math.prod(size) for _, size in parameters])
                self.add_node(node)
                for parameter in source_module.parameters():
                    self.nodes_by_id[id(parameter)] = node
                if not self.criterion_passed and outputs:
                    first = outputs[0]
                    if isinstance(first, list) and first:
                        first = first[0]
                    if hasattr(first, "grad_fn"):
                        self.criterion = first.grad_fn
                del self._pending[identity]
                self._publish()
        return observed

    def _publish(self):
        """Persist an observation snapshot while retaining missing branches.

        Main callers: initialization and forward hooks. The graph lock stabilizes
        simultaneous replicas; the run lock prevents a late callback publishing
        after finish. Finish never takes the graph lock, avoiding lock inversion.
        """
        with self._capture_lock, self._owner_run._lock:
            if self._owner_run._closed:
                return
            self.loaded = not self._pending
            self.metadata["capture"] = {"complete": self.loaded,
                                        "missing_modules": list(self._pending.values())}
            self._owner_run.summary[f"graph_{self._graph_idx}"] = self
