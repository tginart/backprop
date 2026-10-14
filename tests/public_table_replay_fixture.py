"""Create real public Table files for Python/browser replay comparison.

Architecture: SDK serialization -> immutable artifact publication -> public SDK
readback and verified byte fixtures -> browser reconstruction of the same rows.
Main callers: Node replay tests and browser QA. NumPy cases run when installed.
"""
from datetime import datetime,timezone
import base64
import io
import json
import os
import tempfile
import backprop as w
from backprop.object_codec import encode_object
from test_optional_table_serialization import SETTINGS


def fixture():
    try:import numpy as np
    except ImportError:np=None
    sink=w.MemorySink()
    with tempfile.TemporaryDirectory() as directory:
        with w.Run(id='public-replay',entity='e',project='public-tables',dir=directory,_sink=sink,_deployment={},settings=SETTINGS) as run:
            artifact=w.Artifact('public-replay','dataset')
            base=w.Table(columns=['key'],data=[['a']]);base.set_pk('key')
            columns=['time','html','ref']+(['array'] if np is not None else [])
            row=[datetime(2024,1,1,12,0,0,123000,tzinfo=timezone.utc),w.Html('<p>public table</p>',inject=False),base.get_column('key')[0]]
            if np is not None:row.append(np.arange(6,dtype='>i2').reshape(2,3))
            table=w.Table(columns=columns,data=[row]);value=table.to_json(artifact)
            with artifact.new_file('public.table.json',mode='w') as stream:json.dump(value,stream)
            evaluation=w.EvalTable(columns=['input','score'],data=[['a',0.9]],input_columns=['input'],score_columns=['score'])
            with artifact.new_file('evaluation.table.json',mode='w') as stream:json.dump(evaluation.to_json(artifact),stream)
            if np is not None:
                constructed=w.Table(columns=['array'],data=np.arange(8,dtype='i2').reshape(2,1,2,2))
                artifact.add(constructed,'constructed')
            run.log_artifact(artifact).wait()
    saved=w.Api._from_store(sink,entity='e',project='public-tables').artifact('public-replay:latest')
    restored=saved.get('public')
    event=next(e['payload'] for e in sink.events if e['type']=='artifact')
    result={'files':event['files'],'json':{name:json.loads(entry._read_verified_bytes()) for name,entry in saved.manifest.entries.items() if name.endswith('.json')},
        'bytes':{name:base64.b64encode(entry._read_verified_bytes()).decode() for name,entry in saved.manifest.entries.items()},
        'expected':{'time':restored.data[0][0].isoformat(),'html':restored.data[0][1]._bytes().decode(),'ref':str(restored.data[0][2]),'linked_rows':restored.data[0][2]._table.data},'arrays':[]}
    if np is not None:
        result['expected']['array']={'data':restored.data[0][3].tolist(),'shape':list(restored.data[0][3].shape)}
        constructed=saved.get('constructed')
        result['expected']['constructed']=[{'data':row[0].tolist(),'shape':list(row[0].shape),'dtype':str(row[0].dtype)} for row in constructed.data]
        cases={'float16':np.array([[1.5,-2.25]],dtype='f2'),'fortran':np.asfortranarray(np.arange(24).reshape(2,3,4)),
            'big_endian':np.arange(6,dtype='>i2').reshape(2,3),'empty':np.empty((2,0,3)),'scalar':np.array(42),
            'unicode':np.array(['hello','界🙂','a\0b']),'unsigned':np.array([2**64-1],dtype='u8'),'boolean':np.array([True,False]),
            'complex':np.array([1+2j],dtype='c8'),'object':np.array([{'x':1}],dtype=object),'structured':np.array([(1,2)],dtype=[('x','i4'),('y','i4')])}
        for name,array in cases.items():
            stream=io.BytesIO();np.savez_compressed(stream,x=array)
            result['arrays'].append({'name':name,'bytes':base64.b64encode(stream.getvalue()).decode(),'shape':list(array.shape)})
    return result

if __name__=='__main__':
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        print(json.dumps(fixture()))
