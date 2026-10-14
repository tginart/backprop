"""Portable graph values and Keras conversion, backed by existing media files.

Architecture: Node/Edge attributes -> Graph JSON -> Encoder stages immutable
bytes -> Foundry file references -> graph viewer. Keras conversion only inspects
local model topology. PyTorch capture lives in torch_graph.py and owns its hooks.
Main callers: watch(log_graph=True), user Graph construction, Artifact codecs.

Node/Edge and Keras algorithms adapted from W&B 0.30.0 (MIT, see
THIRD_PARTY_NOTICES.md), wandb/sdk/data_types/graph.py.
Upstream SHA-256: fa8caf5c8808c206087509f02533dc18b40ba02f187d3508ada7eba492171320
Foundry differences: graph files support verified artifact reconstruction;
pprint reads the actual attribute dictionary (upstream's stale alias raises).
"""
from __future__ import annotations

import json
import pprint

from .media import EncodedMedia, Media
from .values import WBValue


def _nest(thing):
    # Use tensorflows nest function if available, otherwise just wrap object in an array"""

    try:
        import tensorflow.python.util as tfutil
    except ImportError:
        tfutil = None
    if tfutil:
        return tfutil.nest.flatten(thing)
    else:
        return [thing]


class Edge(WBValue):
    """Edge used in `Graph`."""

    def __init__(self, from_node, to_node):
        self._attributes = {}
        self.from_node = from_node
        self.to_node = to_node

    def __repr__(self):
        temp_attr = dict(self._attributes)
        del temp_attr["from_node"]
        del temp_attr["to_node"]
        temp_attr["from_id"] = self.from_node.id
        temp_attr["to_id"] = self.to_node.id
        return str(temp_attr)

    def to_json(self, run=None):
        return [self.from_node.id, self.to_node.id]

    @property
    def name(self):
        """Optional, not necessarily unique."""
        return self._attributes.get("name")

    @name.setter
    def name(self, val):
        self._attributes["name"] = val
        return val

    @property
    def from_node(self):
        return self._attributes.get("from_node")

    @from_node.setter
    def from_node(self, val):
        self._attributes["from_node"] = val
        return val

    @property
    def to_node(self):
        return self._attributes.get("to_node")

    @to_node.setter
    def to_node(self, val):
        self._attributes["to_node"] = val
        return val


class Node(WBValue):
    """Node used in `Graph`."""

    def __init__(
        self,
        id=None,
        name=None,
        class_name=None,
        size=None,
        parameters=None,
        output_shape=None,
        is_output=None,
        num_parameters=None,
        node=None,
    ):
        self._attributes = {"name": None}
        self.in_edges = {}  # indexed by source node id
        self.out_edges = {}  # indexed by dest node id
        # optional object (e.g. PyTorch Parameter or Module) that this Node represents
        self.obj = None

        if node is not None:
            self._attributes.update(node._attributes)
            del self._attributes["id"]
            self.obj = node.obj

        if id is not None:
            self.id = id
        if name is not None:
            self.name = name
        if class_name is not None:
            self.class_name = class_name
        if size is not None:
            self.size = size
        if parameters is not None:
            self.parameters = parameters
        if output_shape is not None:
            self.output_shape = output_shape
        if is_output is not None:
            self.is_output = is_output
        if num_parameters is not None:
            self.num_parameters = num_parameters

    def to_json(self, run=None):
        return self._attributes

    def __repr__(self):
        return repr(self._attributes)

    @property
    def id(self):
        """Must be unique in the graph."""
        return self._attributes.get("id")

    @id.setter
    def id(self, val):
        self._attributes["id"] = val
        return val

    @property
    def name(self):
        """Usually the type of layer or sublayer."""
        return self._attributes.get("name")

    @name.setter
    def name(self, val):
        self._attributes["name"] = val
        return val

    @property
    def class_name(self):
        """Usually the type of layer or sublayer."""
        return self._attributes.get("class_name")

    @class_name.setter
    def class_name(self, val):
        self._attributes["class_name"] = val
        return val

    @property
    def functions(self):
        return self._attributes.get("functions", [])

    @functions.setter
    def functions(self, val):
        self._attributes["functions"] = val
        return val

    @property
    def parameters(self):
        return self._attributes.get("parameters", [])

    @parameters.setter
    def parameters(self, val):
        self._attributes["parameters"] = val
        return val

    @property
    def size(self):
        return self._attributes.get("size")

    @size.setter
    def size(self, val):
        """Tensor size."""
        self._attributes["size"] = tuple(val)
        return val

    @property
    def output_shape(self):
        return self._attributes.get("output_shape")

    @output_shape.setter
    def output_shape(self, val):
        """Tensor output_shape."""
        self._attributes["output_shape"] = val
        return val

    @property
    def is_output(self):
        return self._attributes.get("is_output")

    @is_output.setter
    def is_output(self, val):
        """Tensor is_output."""
        self._attributes["is_output"] = val
        return val

    @property
    def num_parameters(self):
        return self._attributes.get("num_parameters")

    @num_parameters.setter
    def num_parameters(self, val):
        """Tensor num_parameters."""
        self._attributes["num_parameters"] = val
        return val

    @property
    def child_parameters(self):
        return self._attributes.get("child_parameters")

    @child_parameters.setter
    def child_parameters(self, val):
        """Tensor child_parameters."""
        self._attributes["child_parameters"] = val
        return val

    @property
    def is_constant(self):
        return self._attributes.get("is_constant")

    @is_constant.setter
    def is_constant(self, val):
        """Tensor is_constant."""
        self._attributes["is_constant"] = val
        return val

    @classmethod
    def from_keras(cls, layer):
        node = cls()

        try:
            output_shape = layer.output_shape
        except AttributeError:
            output_shape = ["multiple"]

        node.id = layer.name
        node.name = layer.name
        node.class_name = layer.__class__.__name__
        node.output_shape = output_shape
        node.num_parameters = layer.count_params()

        return node


class Graph(EncodedMedia):
    """W&B class for graphs.

    This class is typically used for saving and displaying neural net models.
    It represents the graph as an array of nodes and edges. The nodes can have
    labels that can be visualized by wandb.

    Attributes:
        format (string): Format to help wandb display the graph nicely.
        nodes ([wandb.Node]): List of `wandb.Nodes`.
        nodes_by_id (dict): dict of ids -> nodes
        edges ([(wandb.Node, wandb.Node)]): List of pairs of nodes interpreted
            as edges.
        loaded (boolean): Flag to tell whether the graph is completely loaded.
        root (wandb.Node): Root node of the graph.

    Examples:
    Import a keras model.

    ```python
    import wandb

    wandb.Graph.from_keras(keras_model)
    ```
    """

    kind = "graph"
    _log_type = "graph-file"

    def __init__(self, format="keras"):
        Media.__init__(self)
        self.path = self.data = self.caption = None
        self.metadata = {"mime_type": "application/json"}
        # LB: TODO: I think we should factor criterion and criterion_passed out
        self.format = format
        self.nodes = []
        self.nodes_by_id = {}
        self.edges = []
        self.loaded = False
        self.criterion = None
        self.criterion_passed = False
        self.root = None  # optional root Node if applicable

    def _to_graph_json(self, run=None):
        # Needs to be its own function for tests
        return {
            "format": self.format,
            "nodes": [node.to_json() for node in self.nodes],
            "edges": [edge.to_json() for edge in self.edges],
        }

    file_is_set = Media.file_is_set
    is_bound = Media.is_bound
    path_is_reference = staticmethod(Media.path_is_reference)

    def _prepare_file(self):
        """Encode the current graph for the ordinary immutable native journal.

        Main callers: Encoder and explicit bind_to_run. Native logging snapshots
        current values independently of the last public bound file; otherwise a
        later node observation could keep uploading a stale earlier snapshot.
        """
        from .plotly_spec import snapshot_bytes
        self.data = snapshot_bytes(self._to_graph_json())
        self.metadata.update(format=self.format, node_count=len(self.nodes), edge_count=len(self.edges))

    def bind_to_run(self, *args, **kwargs):
        """Freeze graph values, preserving the first owner on repeated binding.

        Main callers: explicit public binding. Pinned W&B refreshes a temporary
        file after binding but fails to publish it. Keep its first-owner behavior
        while atomically replacing and publishing that owner's existing path.
        This repair makes the new snapshot recoverable through Foundry journals.
        """
        import os
        import tempfile
        from pathlib import Path
        from .media_files import freeze_file
        self._prepare_file()
        if self.is_bound():
            path = self._path
            # Use the destination volume for atomic replacement, including runs
            # on another mounted disk. The journal freezes its own prior bytes.
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=Path(path).parent, delete=False) as stream:
                    temporary = stream.name
                    stream.write(self.data)
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
            self._set_file(path, is_tmp=False, extension='.graph.json')
            self._run._publish_file(os.path.relpath(path, self._run.dir))
            return
        temporary = freeze_file(self.data, '.graph.json')
        self._set_file(temporary, is_tmp=True, extension='.graph.json')
        Media.bind_to_run(self, *args, **kwargs)

    @classmethod
    def from_json(cls, json_obj, source_artifact):
        """Read graph JSON from verified artifact bytes or a direct graph value.

        Main callers: Artifact.get and WBValue.init_from_json. Reconstruct only
        declared node attributes and edges; logged content cannot import code.
        """
        filename = None
        content_path = None
        if "nodes" not in json_obj:
            path = json_obj.get("artifact_path") or json_obj["path"]
            content_path = path
            from pathlib import Path
            filename = source_artifact.get_entry(path).download()
            json_obj = json.loads(Path(filename).read_bytes())
        graph = cls(format=json_obj["format"])
        for attributes in json_obj["nodes"]:
            node = Node()
            node._attributes = dict(attributes)
            graph.add_node(node)
        for from_id, to_id in json_obj["edges"]:
            graph.add_edge(graph[from_id], graph[to_id])
        if filename is None:
            from .media_files import freeze_file
            graph._prepare_file()
            filename = freeze_file(graph.data, ".graph.json")
            graph._set_file(filename, is_tmp=True, extension=".graph.json")
        else:
            graph._set_file(filename)
        graph._artifact_source = source_artifact
        if content_path is not None:
            graph._artifact_content_path = content_path
        return graph

    @classmethod
    def get_media_subdir(cls):
        """Get media subdirectory.

        "<!-- lazydoc-ignore -->
        """
        return "media/graph"

    def to_json(self, run):
        """Returns the JSON representation expected by the backend.

        <!-- lazydoc-ignore -->
        """
        json_dict = Media.to_json(self, run)
        json_dict["_type"] = self._log_type
        return json_dict

    def __getitem__(self, nid):
        return self.nodes_by_id[nid]

    def pprint(self):
        """Pretty print the graph.

        <!-- lazydoc-ignore -->
        """
        for edge in self.edges:
            pprint.pprint(edge._attributes)  # noqa: T203
        for node in self.nodes:
            pprint.pprint(node._attributes)  # noqa: T203

    def add_node(self, node=None, **node_kwargs):
        """Add a node to the graph.

        <!-- lazydoc-ignore -->
        """
        if node is None:
            node = Node(**node_kwargs)
        elif node_kwargs:
            raise ValueError(
                f"Only pass one of either node ({node}) or other keyword arguments ({node_kwargs})"
            )
        self.nodes.append(node)
        self.nodes_by_id[node.id] = node

        return node

    def add_edge(self, from_node, to_node):
        """Add an edge to the graph.

        <!-- lazydoc-ignore -->
        """
        edge = Edge(from_node, to_node)
        self.edges.append(edge)

        return edge

    @classmethod
    def from_keras(cls, model):
        """Create a graph from a Keras model.

        This method is not supported for Keras 3.0.0 and above.
        Requires a refactor.

        "<!-- lazydoc-ignore -->
        """
        graph = cls()
        # Shamelessly copied (then modified) from keras/keras/utils/layer_utils.py
        sequential_like = cls._is_sequential(model)

        relevant_nodes = None
        if not sequential_like:
            relevant_nodes = []
            for v in model._nodes_by_depth.values():
                relevant_nodes += v

        layers = model.layers
        for i in range(len(layers)):
            node = Node.from_keras(layers[i])
            if hasattr(layers[i], "_inbound_nodes"):
                for in_node in layers[i]._inbound_nodes:
                    if relevant_nodes and in_node not in relevant_nodes:
                        # node is not part of the current network
                        continue
                    for in_layer in _nest(in_node.inbound_layers):
                        inbound_keras_node = Node.from_keras(in_layer)

                        if inbound_keras_node.id not in graph.nodes_by_id:
                            graph.add_node(inbound_keras_node)
                        inbound_node = graph.nodes_by_id[inbound_keras_node.id]

                        graph.add_edge(inbound_node, node)
            graph.add_node(node)
        return graph

    @classmethod
    def _is_sequential(cls, model):
        sequential_like = True

        if (
            model.__class__.__name__ != "Sequential"
            and hasattr(model, "_is_graph_network")
            and model._is_graph_network
        ):
            nodes_by_depth = model._nodes_by_depth.values()
            nodes = []
            for v in nodes_by_depth:
                # TensorFlow2 doesn't insure inbound is always a list
                inbound = v[0].inbound_layers
                if not hasattr(inbound, "__len__"):
                    inbound = [inbound]
                if (len(v) > 1) or (len(v) == 1 and len(inbound) > 1):
                    # if the model has multiple nodes
                    # or if the nodes have multiple inbound_layers
                    # the model is no longer sequential
                    sequential_like = False
                    break
                nodes += v
            if sequential_like:
                # search for shared layers
                for layer in model.layers:
                    flag = False
                    if hasattr(layer, "_inbound_nodes"):
                        for node in layer._inbound_nodes:
                            if node in nodes:
                                if flag:
                                    sequential_like = False
                                    break
                                else:
                                    flag = True
                    if not sequential_like:
                        break
        return sequential_like
