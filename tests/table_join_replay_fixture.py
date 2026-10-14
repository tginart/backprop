"""Build durable cross-artifact joins with typed keys and nested media.

Architecture: independent input artifacts -> real entry references -> destination
copies -> journal publication -> public readback -> Python/browser comparison.
Main callers: test_optional_table_files, Node replay tests and live verification.
"""
from datetime import date,datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
import backprop as w
from backprop.object_codec import encode_object

SETTINGS={'silent':True,'console':'off','disable_git':True,'x_disable_stats':True,'flush_interval':600}


def add_join(run):
    """Capture common and typed key cases in independently logged artifacts.

    Main callers: local fixtures and live Foundry verification. Child media must
    remain available after source artifacts leave the destination reader's view.
    """
    naive=datetime(2024,1,1,0,0,0,123456)
    utc=naive.replace(tzinfo=timezone.utc)
    left_keys=[True,None,'1',date(2024,1,1),naive,utc,('a',True),frozenset([1,'a']),
               (naive,('b',False)),utc+timedelta(microseconds=1),1,('duplicate',),('duplicate',)]
    right_keys=[1,None,'different',date(2024,1,1),naive,utc.astimezone(timezone(timedelta(hours=-5))),
                ('a',1),frozenset(['a',True]),(naive,('b',0)),utc+timedelta(microseconds=2),('duplicate',),('duplicate',)]
    artifacts=[]
    for name,keys in [('left',left_keys),('right',right_keys)]:
        artifact=w.Artifact(run.id+'-'+name,'dataset')
        table=w.Table(columns=['key',name],data=[[key,w.Html('<p>'+name+str(i)+'</p>',inject=False)] for i,key in enumerate(keys)],allow_mixed_types=True)
        artifact.add(table,'nested/'+name);run.log_artifact(artifact).wait()
        artifacts.append(artifact)
    joined=w.JoinedTable(artifacts[0].get_entry('nested/left.table.json'),artifacts[1].get_entry('nested/right.table.json'),'key')
    # This also verifies direct materialization calls the actual parent accessor.
    direct=joined.materialize()
    target=w.Artifact(run.id+'-copy','dataset');target.add(joined,'joined');run.log_artifact(target).wait()
    run.log({'joined':joined})
    return target,direct


def fixture():
    with tempfile.TemporaryDirectory() as directory:
        sink=w.MemorySink()
        with w.Run(id='join-replay',entity='e',project='joins',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
            target,direct=add_join(run)
        api=w.Api._from_store(sink,entity='e',project='joins')
        saved=api.artifact(target.id)
        restored=saved.get('joined').materialize()
        assert restored.columns==direct.columns
        assert [[row[1]._bytes(),row[3]._bytes()] for row in restored.data if row[1] and row[3]]==[[row[1]._bytes(),row[3]._bytes()] for row in direct.data if row[1] and row[3]]
        # Compare only key and text output cells, so browser file transport IDs
        # cannot obscure whether the actual Python join selected identical rows.
        def display(table):
            data=[]
            for row in table.data:
                data.append([None if cell is None else cell._bytes().decode() for cell in [row[1],row[3]]])
            return data
        files=next(e['payload']['files'] for e in sink.events if e['type']=='artifact' and e['payload']['artifact_id']==target.id)
        values={entry.path:json.loads(entry._read_verified_bytes()) for entry in saved.manifest.entries.values() if entry.path.endswith('.json')}
        content={entry.path:entry._read_verified_bytes().decode() for entry in saved.manifest.entries.values() if not entry.path.endswith('.json')}
        return {'files':files,'json':values,'media':content,'expected':display(restored),'events':sink.events,'artifact_id':target.id}

if __name__=='__main__':print(json.dumps(fixture()))
