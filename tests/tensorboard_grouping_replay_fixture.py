"""Portable grouped-source protocol fixtures with real independent journals.

Architecture: labelled source positions -> approved group sidecars -> ordinary
Run.log into independent MemorySinks -> raw events plus manually expected rows.
Main callers: focused Python/browser replay tests. Why this exists: delivery
order, missing observations and conflicts are storage boundaries, independent
of optional TensorBoard availability. Source byte labels are protocol fixtures;
they do not claim real EventFileWriter or converter parity.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import backprop
from backprop.integration.tensorboard.grouping import closed_groups, history_metrics
from backprop.integration.tensorboard.origin import event_origin, record_identity_from_origin, stabilize_table_ids

# Valid RGB PNG, 2x3 pixels of (17, 34, 51). Keep the actual encoded image
# constant so storage/provenance fixtures do not require optional Pillow.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000002000000030802000000368849d6"
    "0000001449444154789c631454326660606062000328050007c4006c33fa76db"
    "0000000049454e44ae426082"
)


def member(index, step, metrics=None, *, filename="events.out.tfevents.1.a", wall_time=None):
    raw = json.dumps([filename, index, step, wall_time], separators=(",", ":")).encode()
    return {"origin": event_origin("", filename, raw), "index": index, "step": str(step),
            "wall_time": index + 100. if wall_time is None else wall_time, "metrics": metrics or {}}


def journal(groups, label, directory, *, legacy=(), definitions=(), after=()):
    """Encode one importer view in its own actual run journal and file store.

    Main caller: fixtures. Different views share run scope but retain physical
    writer identity and sequences. Rich objects are copied before encoding, so
    prior implicit-artifact bindings cannot contaminate another store's view.
    """
    sink = backprop.MemorySink()
    with backprop.Run(id="group-replay", entity="e", project="tb-group", mode="shared",
        dir=directory, _sink=sink, _deployment={}, settings={"silent": True, "console": "off",
            "disable_git": True, "x_disable_stats": True, "x_label": label, "x_primary": False}) as run:
        for key, summary in definitions:
            run.define_metric(key, summary=summary)
        for source in legacy:
            origin = source["origin"]
            run.log({**source["metrics"], "global_step": int(source["step"]), "_timestamp": source["wall_time"],
                "_tensorboard_record": record_identity_from_origin(origin), "_tensorboard_origin": origin,
                "_tensorboard_source": origin["filename"]})
        for group in copy.deepcopy(groups):
            for source in group["members"]:
                stabilize_table_ids(source["metrics"], record_identity_from_origin(source["origin"]))
            run.log(history_metrics(group))
        for action, value in after:
            if action == "group":
                run.log(history_metrics(value))
            elif action == "log":
                run.log(value)
            elif action == "summary_update":
                run.summary.update(value)
            elif action == "summary_delete":
                del run.summary[value]
            else:
                raise ValueError("Unknown grouping fixture action")
    return {"events": sink.events, "files": {name: data.hex() for name, data in sink.files.items()}}


def fixtures(output=None):
    """Build source-order expectations independently of replay implementation.

    Main callers: Python unit tests and the browser fixture driver. Full rows
    exclude private provenance/runtime fields; explicit incompleteness flags
    remain assertions when an ordinal or decoded boundary member is missing.
    """
    cases = {}
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BACKPROP_DIR": directory}):
        def add(name, views, expected, *, legacy=(), conflict=False, definitions=(), after=(), summary=None):
            stores = [journal(groups, name + "-" + str(index), directory,
                              legacy=legacy if index == 0 else (), definitions=definitions,
                              after=after if index == 0 else ()) for index, groups in enumerate(views)]
            if summary is None:
                summary = {key: value for row in expected for key, value in row.items()
                           if key != "_tensorboard_incomplete"}
            cases[name] = {"events": [event for store in stores for event in store["events"]],
                "stores": stores, "files": {key: value for store in stores for key, value in store["files"].items()},
                "expected_history": expected, "expected_summary": summary,
                "expected_writer_count": len(stores), "expect_conflict": conflict}

        source = [member(0, 0), member(1, 5, {"a": 1}), member(2, 5, {"sparse": 2}),
                  member(3, 5, {"a": 3}), member(4, 7, {"z": 4})]
        prefix = list(closed_groups(source[:2], final=True))
        full = list(closed_groups(source, final=True))
        branch = list(closed_groups(source[:3], final=True))
        add("revision_before_prefix", [full, prefix, branch], [
            {"a": 3, "sparse": 2, "global_step": 5, "_timestamp": 103.},
            {"z": 4, "global_step": 7, "_timestamp": 104.}])
        expected = [{"a": 3, "sparse": 2, "global_step": 5, "_timestamp": 103.},
                    {"z": 4, "global_step": 7, "_timestamp": 104.}]
        base_summary = {key: value for row in expected for key, value in row.items()}
        definitions = [("a", "min,max,mean,last")]
        add("summary_custom_revisions", [full, prefix, branch], expected, definitions=definitions,
            summary={**base_summary, "a": {"min": 3, "max": 3, "mean": 3, "last": 3}})
        add("summary_manual_update", [full], expected,
            after=[("summary_update", {"a": 123, "manual_only": 456}), ("group", prefix[0])],
            summary={**base_summary, "a": 123, "manual_only": 456})
        add("summary_manual_delete", [full], expected,
            after=[("summary_delete", "a"), ("group", prefix[0])],
            summary={key: value for key, value in base_summary.items() if key != "a"})
        ordinary = {"a": 99, "global_step": 99, "_timestamp": 999.}
        add("ordinary_colliding_key", [full], [*expected, ordinary],
            after=[("log", ordinary), ("group", prefix[0])], summary={**base_summary, **ordinary})
        add("summary_custom_ordinary", [full], [*expected, ordinary], definitions=definitions,
            after=[("log", ordinary), ("group", prefix[0])],
            summary={**base_summary, **ordinary, "a": {"min": 3, "max": 99, "mean": 51, "last": 99}})

        # Foundry projection rule: an extended source group revises its original
        # logical slot. Explicit edits after that slot remain authoritative;
        # a genuinely new logical group is a later summary sample.
        revised_source = source + [member(5, 7, {"z": 8, "a": 5})]
        revised = list(closed_groups(revised_source, final=True))[-1]
        revised_rows = [expected[0], {"z": 8, "a": 5, "global_step": 7, "_timestamp": 105.}]
        revised_summary = {key: value for row in revised_rows for key, value in row.items()}
        add("source_revision_after_manual_update", [full], revised_rows,
            after=[("summary_update", {"a": 123, "manual_only": 456}), ("group", revised)],
            summary={**revised_summary, "a": 123, "manual_only": 456})
        add("source_revision_after_manual_delete", [full], revised_rows,
            after=[("summary_delete", "a"), ("group", revised)],
            summary={key: value for key, value in revised_summary.items() if key != "a"})
        extended = list(closed_groups(revised_source + [member(6, 9, {"a": 9})], final=True))
        new_row = {"a": 9, "global_step": 9, "_timestamp": 106.}
        add("new_logical_row_after_manual_update", [full], [*revised_rows, new_row],
            after=[("summary_update", {"a": 123, "manual_only": 456}),
                   ("group", extended[-2]), ("group", extended[-1])],
            summary={**revised_summary, **new_row, "manual_only": 456})

        a = [member(0, 0, wall_time=100.), member(1, 0, {"a": 1}, wall_time=101.)]
        b = [member(0, 0, filename="events.out.tfevents.2.b", wall_time=200.),
             member(1, 1, {"middle": 2}, filename="events.out.tfevents.2.b", wall_time=201.)]
        c = [member(0, 0, filename="events.out.tfevents.3.c", wall_time=300.),
             member(1, 0, {"c": 3}, filename="events.out.tfevents.3.c", wall_time=301.)]
        add("divergent_files_new_barrier", [list(closed_groups(a + c, final=True)),
            list(closed_groups(b + c, final=True)), list(closed_groups(a + b + c, final=True))], [
                {"a": 1, "global_step": 0, "_timestamp": 200.},
                {"middle": 2, "global_step": 1, "_timestamp": 201.},
                {"c": 3, "global_step": 0, "_timestamp": 301.}])

        first = [member(0, 0), member(1, 5, {"a": 1})]
        last = [member(3, 5, {"b": 3})]
        add("missing_ordinal", [list(closed_groups(first, final=True)), list(closed_groups(last, final=True))], [
            {"a": 1, "global_step": 5, "_timestamp": 101., "_tensorboard_incomplete": True},
            {"b": 3, "global_step": 5, "_timestamp": 103., "_tensorboard_incomplete": True}])
        complete = list(closed_groups(first + [member(2, 7, {"middle": 2})] + last, final=True))
        add("missing_ordinal_filled", [list(closed_groups(first, final=True)),
            list(closed_groups(last, final=True)), complete], [
                {"a": 1, "global_step": 5, "_timestamp": 101.},
                {"middle": 2, "global_step": 7, "_timestamp": 102.},
                {"b": 3, "global_step": 5, "_timestamp": 103.}])
        add("boundary_only", [[complete[0], complete[2]]], [
            {"a": 1, "global_step": 5, "_timestamp": 101.},
            {"b": 3, "global_step": 5, "_timestamp": 103., "_tensorboard_incomplete": True}])

        migration = [member(0, 0), member(1, 5, {"a": 1}), member(2, 5, {"b": 2})]
        add("old_v1_migration", [list(closed_groups(migration, final=True))], [
            {"a": 1, "b": 2, "global_step": 5, "_timestamp": 102.}], legacy=migration[1:2])
        exact = [member(0, 0)] + [member(index + 1, step, {"a": index}) for index, step in enumerate(
            [2**53, 2**53 + 1, -(2**63), -(2**63) + 1])]
        add("exact_int64_steps", [list(closed_groups(exact, final=True))], [
            {"a": index, "global_step": int(source["step"]), "_timestamp": source["wall_time"]}
            for index, source in enumerate(exact[1:])])

        import hashlib
        data = PNG_BYTES
        descriptor = {"_type": "image", "path": "original.png", "name": "original.png",
                      "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "width": 2, "height": 3}
        rich = [member(0, 0), member(1, 5, {"image": [descriptor], "loss": 1})]
        add("rich_relocation", [list(closed_groups(rich, final=True)), list(closed_groups(rich, final=True))], [
            {"image": [{key: value for key, value in descriptor.items() if key not in {"path", "name"}}],
             "loss": 1, "global_step": 5, "_timestamp": 101.}])
        case = cases["rich_relocation"]
        relocated = case["stores"][1]["events"]
        for event in relocated:
            if event["type"] != "history":
                continue
            metrics = event["payload"]["metrics"]
            for image in (metrics["image"][0], metrics["_tensorboard_group"]["members"][0]["metrics"]["image"][0]):
                image["path"], image["name"] = "relocated.png", "relocated.png"
        case["events"] = [event for store in case["stores"] for event in store["events"]]
        conflict = copy.deepcopy(case)
        conflict["expect_conflict"] = True
        for event in conflict["stores"][1]["events"]:
            if event["type"] != "history":
                continue
            metrics = event["payload"]["metrics"]
            metrics["image"][0]["sha256"] = "0" * 64
            metrics["_tensorboard_group"]["members"][0]["metrics"]["image"][0]["sha256"] = "0" * 64
        conflict["events"] = [event for store in conflict["stores"] for event in store["events"]]
        cases["rich_conflict"] = conflict
    result = {"protocol_version": 1, "source_kind": "source-backed protocol fixtures with real native journals",
              "cases": cases}
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    import sys
    destination = sys.argv[1] if len(sys.argv) > 1 else "test-results/tensorboard-grouping-replay.json"
    print(json.dumps({"path": destination, "cases": list(fixtures(destination)["cases"])}))
