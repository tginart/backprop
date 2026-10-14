# Distributed logging

Use shared mode when processes you launch contribute to one experiment. Backprop uses
the existing Foundry event stream, file dataset and durable local journals. It does not
start workers, choose a leader or schedule experiments.

## Open a shared run

Give every process the same entity, project and run ID. Assign one primary and a stable,
unique label to each logical writer:

```python
import backprop as wandb

run = wandb.init(
    entity="team", project="training", id="experiment-42", mode="shared",
    settings=wandb.Settings(x_label=f"rank-{rank}", x_primary=(rank == 0)),
)
run.define_metric("tokens")
run.define_metric("train/*", step_metric="tokens")
run.log({"tokens": tokens_processed, "train/loss": loss})
run.finish()
```

`rank`, `tokens_processed` and `loss` come from your training program. Every writer has
its own journal, identity, sequence and local steps. A nonempty `x_label` allows a
restarted handle to find its pending artifact contribution. An absent or empty label
creates an independent writer; recover its interrupted journal explicitly with `python
-m backprop sync PATH`.

The designated primary controls final experiment state. Secondary completion does not
imply that the whole experiment has finished. Coordinate the primary's finish with your
own process barrier if you need all workers to finish first. Offline writers each need
their own sync; uploading one journal cannot upload another machine's data.

## Reads and conflicting edits

SDK and browser replay use UTC microseconds and event IDs for the same ordering. Retries
retain their event IDs, so duplicate delivery does not add history rows. Configuration
and summary updates replace their named root keys; unrelated keys survive. Concurrent
edits to the same root resolve by event order, rather than a nested-field transaction.
Public summary edits form an inspection overlay.

Writer-local step numbers stay distinct. Declare a common training axis, such as
`tokens`, to compare writers. The viewer does not combine different writers only because
their local steps match. Grouped charts align exact recorded coordinates; they do not
reproduce W&B server bucketing or interpolate missing contributions. Unfinished writers,
sequence conflicts and missing records remain visible.

Foundry reads converge as events become visible. A snapshot cannot prove that another
writer has not published data which is still unavailable to that reader. There is no
immediate cross-client consistency guarantee.

## Contribute and finalize artifacts

Workers call `run.upsert_artifact(artifact)` for immutable contributions. The primary
calls `run.finish_artifact(artifact)` to contribute its files and finalize the visible
union. Conflicting bytes at the same path raise an error.

Contributions and finalization are journaled. Offline sync selects visible, unconsumed
parts and stores the chosen manifest in the existing file dataset. Copied journals and
restarted finalizers reuse that immutable choice. Late parts stay pending for the next
finalization; retries do not add them to an already committed version. The caller must
coordinate completeness before finalizing.

`artifact.wait()` on an upsert confirms contribution acceptance and may return a
`PENDING` object. After its completion becomes visible, another wait returns the
combined committed files. Independent runs currently require explicit `log_artifact()`
to record the completed union as an output relationship. See the [API support
guide](python-api-support.md) for these handle and publication rules.

## Cooperative stop and framework workers

Public `Run.stop()` requests every running or pending writer visible in the snapshot. A
writer discovered later needs another call. Requests retain their identity across
retries. Acknowledgment means the callback accepted the request, not that training
stopped. The primary still owns final experiment state. The browser's stop dialog
targets the advertised primary process separately.

Lightning Fabric's logger restores one experiment ID into spawned workers with separate
journals; the parent finishes the experiment. Multi-device GPU capture is experimental.
Optional runtime requirements are declared in [pyproject.toml](../pyproject.toml); see
the [API support guide](python-api-support.md) for the supported boundaries.
