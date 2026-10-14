"""Read actual local run histories for integration-switch runtime probes.

Architecture: finished real run -> public reference service teardown -> one
immutable transaction capture and shared checked decoder -> history rows.
Main callers: framework_init_probe writer and history reader cases. Why this
exists: run.finish can precede the core transaction writer's final flush; the
shared lifecycle reader captures bytes only after the service has closed.
"""
from pathlib import Path


def histories(run, reference, *, reference_output=None):
    """Read the SDK's real history bytes after normal run.finish completes.

    Main callers: actual TensorBoard writer and dependency-free history probes.
    The reference service closes before its immutable capture. Optional raw
    evidence uses those exact same bytes; header and CRC checks remain strict.
    """
    if not reference:
        from backprop.transport import read_journal
        return [event['payload']['metrics'] for event in read_journal(run.sync_dir)
                if event['type'] == 'history']
    from tensorboard_gap_transaction import completed_reference_records
    from tensorboard_gap_observations import history_row
    records, transaction_bytes = completed_reference_records(run)
    if reference_output is not None:
        Path(reference_output).write_bytes(transaction_bytes)
    return [history_row(record.history.item) for record in records
            if record.WhichOneof('record_type') == 'history']
