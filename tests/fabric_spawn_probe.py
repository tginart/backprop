"""Exercise real Fabric spawning twice with one native logger/experiment.

Architecture: parent logger -> Fabric's actual DDP spawn launcher -> rank-zero
secondary writer -> merged native journals and deterministic replay. Main
caller: test_optional_fabric_spawn in lower/current isolated CI. Launch follows
https://lightning.ai/docs/fabric/2.4.0/api/generated/lightning.fabric.fabric.Fabric.html.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys


def train(fabric, directory, iteration):
    import os
    import torch
    import backprop
    from backprop.transport import read_journal
    model=torch.nn.Linear(2,1)
    optimizer=torch.optim.SGD(model.parameters(),lr=.01)
    model,optimizer=fabric.setup(model,optimizer)
    for step in range(2):
        optimizer.zero_grad()
        loss=model(torch.ones(1,2)).square().mean()
        fabric.backward(loss);optimizer.step()
        fabric.log_dict({f'round{iteration}/loss':float(loss.detach())},step=step)
    logger=fabric.logger
    logger.save();logger.finalize('success')
    if fabric.global_rank==0:
        assert logger.experiment.config['lr']==.01
        run=logger.experiment
        run.config.update({f'round{iteration}':True})
        identity=run.id;writer=run._writer_id;scope=run.path
        run.finish()
        assert all(e['type']!='run_finish' for e in read_journal(run.sync_dir))
    else:
        assert logger._experiment is None
        identity=logger.version;writer=None;scope=None
    Path(directory,f'rank-{fabric.global_rank}-round-{iteration}.json').write_text(json.dumps(
        {'rank':fabric.global_rank,'pid':os.getpid(),'id':identity,'writer':writer,'scope':scope}))
    fabric.barrier()
    backprop.teardown()


def main(directory, mode):
    import backprop
    from backprop.apis.public.replay import reduce_events
    from backprop.integration.lightning.fabric import WandbLogger
    from backprop.transport import MemorySink,read_journal
    from lightning.fabric import Fabric
    options={'entity':'e','project':'fabric-spawn','save_dir':directory,'config':{'lr':.01},
        'mode':mode,'settings':{'silent':True,'console':'off','disable_git':True,'x_disable_stats':True}}
    if mode=='online':options['_sink']=MemorySink()
    logger=WandbLogger(**options)
    assert logger.version is None
    rounds=[]
    try:
        for iteration in range(2):
            fabric=Fabric(accelerator='cpu',devices=2,strategy='ddp_spawn',loggers=logger)
            fabric.launch(train,directory,iteration)
            rounds.extend(json.loads(path.read_text()) for path in sorted(Path(directory).glob(f'rank-*-round-{iteration}.json')))
        identity=logger.version
        assert identity and all(row['id']==identity for row in rounds)
        assert len({row['writer'] for row in rounds if row['rank']==0})==2
        assert all(row['writer'] is None for row in rounds if row['rank']==1)
        def events():
            return [event for path in Path(directory).rglob('events.jsonl') for event in read_journal(path.parent)]
        assert reduce_events(events())['state']=='running'
        logger.log_metrics({'parent_after':1},step=9)
        logger.experiment.finish()
        records=events();state=reduce_events(records)
        assert state==reduce_events(list(reversed(records))+records)
        assert state['state']=='finished'
        assert all(f'round{iteration}/loss' in state['summary'] for iteration in range(2))
        assert state['summary']['parent_after']==1
        assert state['config']['round0'] and state['config']['round1']
        assert len(state['writers'])==3
        # Each worker logs two training rows and save() commits a third row.
        # W&B 0.30.0's logger.save is log({}, commit=True), not just flush().
        assert len(state['history'])==7, state['history']
        for iteration in range(2):
            measured=[row for row in state['history'] if f'round{iteration}/loss' in row]
            assert len(measured)==2
            assert [row['trainer/global_step'] for row in measured]==[0,1]
        for row in rounds:
            if row['rank']==0:
                assert [item['_step'] for item in state['history'] if item.get('_writer')==row['writer']]==[0,1,2]
        print(json.dumps({'mode':mode,'id':identity,'state':state['state'],'writers':3,
            'history_rows':len(state['history']),'rounds':rounds,'summary':state['summary']}),flush=True)
    finally:backprop.teardown()


if __name__=='__main__':main(*sys.argv[1:])
