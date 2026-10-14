"""Real TensorBoard event fixtures shared by tests and the isolated SDK probe.

Architecture: genuine protobuf summaries and EventFileWriter files -> native or
pinned reader -> normalized scalar/histogram/image/PR data. Main callers:
test_optional_tensorboard_gaps and tensorboard_gap_probe. No framework package
is simulated; callers skip explicitly when its optional dependencies are absent.
"""
import io
from pathlib import Path


def png():
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (2, 3), (17, 34, 51)).save(buffer, format="PNG")
    return buffer.getvalue()


def tensor_value(tag, data, plugin):
    from tensorboard.compat.proto.summary_pb2 import Summary, SummaryMetadata
    from tensorboard.util.tensor_util import make_tensor_proto
    return Summary.Value(tag=tag, tensor=make_tensor_proto(data),
        metadata=SummaryMetadata(plugin_data=SummaryMetadata.PluginData(plugin_name=plugin)))


def events():
    """Cover active pinned converters and omitted metadata in subsequent events.

    Main callers: real-writer tests and both isolated SDK processes. PR rows keep
    their source order and zero endpoints, matching the active Go converter.
    """
    import numpy as np
    from tensorboard.compat.proto.event_pb2 import Event
    from tensorboard.compat.proto.summary_pb2 import Summary, HistogramProto
    first = [tensor_value("loss", 0.25, "scalars"),
             tensor_value("weights", [[0., 1., 2.], [1., 2., 3.]], "histograms"),
             Summary.Value(tag="legacy_weights", histo=HistogramProto(min=0, max=2,
                 bucket_limit=[0, 1, 2], bucket=[0, 2, 3])),
             tensor_value("frame", [b"2", b"3", png()], "images"),
             tensor_value("curve", np.array([[0., 0., 0.]] * 4 + [[.75, .5, 0.], [.25, .75, 0.]]), "pr_curves")]
    second = [tensor_value("loss", 0.125, ""),
              tensor_value("weights", [[0., 1., 4.], [1., 2., 1.]], "")]
    return [Event(step=8, wall_time=100., summary=Summary(value=first)),
            Event(step=9, wall_time=101., summary=Summary(value=second))]


def encoded_image(format_name, color=(17, 34, 51), animated=False):
    from PIL import Image
    buffer = io.BytesIO()
    image = Image.new("RGB", (2, 3), color)
    options = {"save_all": True, "append_images": [Image.new("RGB", (2, 3), (52, 68, 85))],
               "duration": 50, "loop": 0} if animated else {}
    image.save(buffer, format=format_name, **options)
    return buffer.getvalue()


def extended_events():
    """Exercise additional supported active-core histogram and image forms.

    Main callers: isolated runtime comparisons and genuine native writer tests.
    JPEG batches and animated GIFs retain their exact encoded bytes; legacy
    empty and nonzero-first-bucket histograms follow the active Go converter.
    """
    from tensorboard.compat.proto.event_pb2 import Event
    from tensorboard.compat.proto.summary_pb2 import Summary, HistogramProto
    values = [Summary.Value(tag="empty_histogram", histo=HistogramProto(bucket_limit=[0], bucket=[0])),
              Summary.Value(tag="nonempty_histogram", histo=HistogramProto(min=0, max=2,
                  bucket_limit=[1, 2], bucket=[2, 3])),
              Summary.Value(tag="legacy_png", image=Summary.Image(width=2, height=3,
                  colorspace=3, encoded_image_string=png())),
              tensor_value("jpeg_frames", [b"2", b"3", encoded_image("JPEG"),
                  encoded_image("JPEG", (52, 68, 85))], "images"),
              tensor_value("gif_frames", [b"2", b"3", encoded_image("GIF", animated=True)], "images")]
    return [Event(step=12, wall_time=140., summary=Summary(value=values))]


def unsupported_events():
    """Produce genuine graph, run-metadata and plugin boundary messages.

    Main caller: the isolated pinned/native omission probe. Graphs use an
    actual GraphDef node; hparams use TensorBoard's own public protobuf writer.
    The active pinned core does not convert these fields/plugins. Native errors
    are checked separately so an upstream omission is never counted as support.
    """
    from tensorboard.compat.proto.event_pb2 import Event
    from tensorboard.compat.proto.graph_pb2 import GraphDef
    from tensorboard.plugins.hparams.summary_v2 import hparams_pb
    from tensorboard.compat.proto.summary_pb2 import Summary
    graph = GraphDef()
    graph.node.add(name="node", op="NoOp")
    return {"graph": Event(step=1, wall_time=140., graph_def=graph.SerializeToString()),
            "run_metadata": Event(step=1, wall_time=140., tagged_run_metadata={"tag": "profile", "run_metadata": b""}),
            "hparams": Event(step=1, wall_time=140., summary=hparams_pb(
                {"learning_rate": .1}, trial_id="trial", start_time_secs=140.)),
            "unknown": Event(step=1, wall_time=140., summary=Summary(value=[tensor_value("unknown", 1., "unknown")]))}


def integer_events():
    """Use actual DT_INT64 scalar summaries around the float64 boundary.

    Main caller: the isolated precision probe. Named scalars round through the
    active pinned Tensor.Scalar float64 conversion; Event.step is a separate
    signed-int64 field. Each step and tag stays distinct so source grouping
    cannot conceal a boundary value. Both positive and negative sides are used.
    """
    import numpy as np
    from tensorboard.compat.proto.event_pb2 import Event
    from tensorboard.compat.proto.summary_pb2 import Summary
    boundaries = [2**53 - 1, 2**53, 2**53 + 1, -(2**53 - 1), -2**53, -(2**53 + 1)]
    return [Event(step=value, wall_time=200. + index,
                  summary=Summary(value=[tensor_value("int64_" + str(index), np.int64(value), "scalars")]))
            for index, value in enumerate(boundaries)]


def write(directory, source_events=None):
    from tensorboard.summary.writer import event_file_writer
    writer = event_file_writer.EventFileWriter(str(directory))
    for event in events() if source_events is None else source_events:
        writer.add_event(event)
    writer.flush()
    writer.close()
    return next(Path(directory).glob("*tfevents*"))


def delayed_duplicate_import(output, large_pr=False):
    """Produce genuine duplicate source observations from two remote writers.

    Main caller: the parent's Python/browser replay coordination tests. Each run
    uses an independent local journal. The fake backend hides history reads, an
    unavoidable storage visibility boundary, while event files and both Run.log
    calls are real. Exact source identities should yield one reconstructed row.
    """
    import json
    import os
    import tempfile
    from unittest.mock import patch
    import backprop
    from backprop.integration import tensorboard
    from backprop.transport import MemorySink

    class DelayedSink(MemorySink):
        def read_events(self, **scope):
            return [event for event in super().read_events(**scope) if event["type"] != "history"]

    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
        source = Path(directory) / "source"
        source_events = events()[:1]
        if large_pr:
            import numpy as np
            curve = np.array([[0.] * 6000] * 4 + [
                [(6000 - index) / 6001 for index in range(6000)],
                [index / 6001 for index in range(6000)]])
            source_events[0].summary.value[-1].CopyFrom(tensor_value("curve", curve, "pr_curves"))
        write(source, source_events)
        stores = []
        for label in ("worker-a", "worker-b"):
            sink = DelayedSink()
            run = backprop.Run(id="tb-duplicate", entity="e", project="tb-gap", mode="shared", _sink=sink,
                dir=directory, settings={"silent": True, "console": "off", "x_disable_stats": True,
                                        "x_label": label, "x_primary": False, "flush_interval": 3600})
            tensorboard.sync(run, source)
            run.finish()
            stores.append({"events": sink.events, "files": {
                name: data.hex() for name, data in sink.files.items()}})
        all_events = [event for store in stores for event in store["events"]]
        history = [event for event in all_events if event["type"] == "history"]
        Path(output).write_text(json.dumps({"events": all_events, "stores": stores,
            "files": {name: data for store in stores for name, data in store["files"].items()},
            "physical_history_count": len(history),
            "expected_reconstructed_history_count": 1,
            "record_keys": [event["payload"]["metrics"]["_tensorboard_record"] for event in history]}, indent=2) + "\n")
        return history
