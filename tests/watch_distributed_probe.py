"""One real Gloo/DDP worker comparing pinned and Backprop measurements.

Architecture: two local workers share a process group -> identical reference and
native DDP training -> native shared-run journals -> saved event/file outputs.
Main caller: test_optional_watch. The parent combines delayed, reordered worker
events and checks public reads. No external service, scheduler or GPU is needed.
"""
from datetime import timedelta
import base64
import json
from pathlib import Path
import sys

import backprop
import torch

from watch_reference_probe import recorder


def train(rank, directory):
    """Execute two matching training sessions inside the existing process group.

    Main caller: worker entry point. Each local step remains attached to its
    writer; the explicit epoch axis is shared across workers for comparisons.
    """
    torch.set_num_threads(1)
    expected = []
    watch, unwatch, events, _ = recorder(False)
    torch.manual_seed(34)
    reference = torch.nn.parallel.DistributedDataParallel(torch.nn.Linear(2, 1))
    watch(reference, log='all', log_freq=1, idx=0)
    watch(reference, log='all', log_freq=2, idx=1)
    for step in range(2):
        reference.zero_grad()
        reference(torch.ones(2, 2) * (rank + 1)).sum().backward()
        row = {}
        for item in events:
            row.update(item)
        expected.append(row)
        events.clear()
    unwatch(reference)
    torch.distributed.barrier()
    torch.manual_seed(34)
    model = torch.nn.parallel.DistributedDataParallel(torch.nn.Linear(2, 1))
    sink = backprop.MemorySink()
    run = backprop.Run(id='distributed-watch', project='watch', entity='e', mode='shared',
                       dir=str(directory / f'rank-{rank}'), _sink=sink,
                       settings={'x_label': f'rank-{rank}', 'x_primary': rank == 0,
                                 'console': 'off', 'silent': True, 'x_disable_stats': True})
    try:
        run.define_metric('*', step_metric='epoch')
        run.watch(model, log='all', log_freq=1, idx=0, log_graph=True)
        run.watch(model, log='all', log_freq=2, idx=1)
        for step in range(2):
            model.zero_grad()
            model(torch.ones(2, 2) * (rank + 1)).sum().backward()
            run.log({'rank': rank, 'epoch': step})
        run.unwatch([])
        assert run._watcher.models == {}
        assert run._watcher._gradient_handles == {}
        model(torch.ones(2, 2)).sum().backward()
    finally:
        run.finish()
    return {'rank': rank, 'expected': expected, 'events': sink.events,
            'files': {name: base64.b64encode(value).decode() for name, value in sink.files.items()}}


if __name__ == '__main__':
    rank, directory = int(sys.argv[1]), Path(sys.argv[2])
    torch.distributed.init_process_group('gloo', init_method=(directory / 'rendezvous').as_uri(),
                                        rank=rank, world_size=2, timeout=timedelta(seconds=20))
    try:
        result = train(rank, directory)
        (directory / f'result-{rank}.json').write_text(json.dumps(result))
    finally:
        torch.distributed.destroy_process_group()
