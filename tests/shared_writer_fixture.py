"""Independent-process shared logging fixture, with no credentials or network.

Architecture: process arguments -> separate writer journal -> captured envelopes.
Main callers: test_sdk_shared_processes; parent replays the combined envelopes.
"""
import json
from pathlib import Path
import sys
import backprop
from backprop.transport import MemorySink

root,label,primary,output=sys.argv[1:]
sink=MemorySink()
with backprop.Run(entity='e',project='p',id='shared',mode='shared',dir=root,_sink=sink,
                  settings={'x_label':label,'x_primary':primary=='true','console':'off','silent':True,'x_disable_stats':True}) as run:
    run.config[label]=True
    run.define_metric('epoch')
    run.define_metric(label+'/*',step_metric='epoch')
    for epoch in range(3):run.log({'epoch':epoch,label+'/loss':3-epoch})
Path(output).write_text(json.dumps(sink.events))
