/**
 * Architecture: fixture REST endpoints -> real API/auth/model/UI -> browser QA.
 * Runs with an existing Playwright package and installed Chromium-family binary.
 * No packages or browsers are installed. The isolated browser has only a fake
 * fixture token. Screenshots are visibly labeled Synthetic demo.
 * Main caller: node tests/browser_qa.mjs (optional local browser verification).
 * Optional PLAYWRIGHT_MODULE / CHROMIUM_EXECUTABLE override existing runtimes.
 */
import assert from 'node:assert/strict';
import {navigationFixtures,verifyNavigationUI} from './ui_navigation_qa.mjs';
import {sklearnFixture,verifySklearnUI} from './ui_sklearn_qa.mjs';
import {analysisFixtures,verifyAnalysisUI} from './ui_analysis_qa.mjs';
import { createServer } from 'node:http';
import { spawnSync } from 'node:child_process';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { join, extname } from 'node:path';
import { deflateSync } from 'node:zlib';
const root = fileURLToPath(new URL('../', import.meta.url));
const playwright = await import(process.env.PLAYWRIGHT_MODULE || 'playwright');
const host = 'https://foundry-test.invalid';
const config = { host, client_id: 'test-client', stream_dataset_rid: 'ri.foundry.main.dataset.test-stream', media_dataset_rid: 'ri.foundry.main.dataset.test-media', branch: 'master', map_resource_origins:['https://tile.openstreetmap.org'] };
// Exercise the exact release policy instead of a looser development document.
// Python receives only the test host and public HTML, never a credential.
const policyResult = spawnSync(process.env.PYTHON || 'python3', ['-c',
  'import sys; sys.path.insert(0, sys.argv[1]); from deploy import apply_content_security_policy; sys.stdout.buffer.write(apply_content_security_policy(sys.stdin.buffer.read(), {"host": sys.argv[2],"map_resource_origins":["https://tile.openstreetmap.org"]}))',
  join(root, 'deploy'), host], { input: await readFile(join(root, 'web/index.html')), encoding: 'utf8' });
assert.equal(policyResult.status, 0, `Release policy generation failed: ${policyResult.stderr}`);
const releaseHtml = policyResult.stdout;
assert(releaseHtml.includes('Content-Security-Policy'), 'The browser fixture must use the release CSP');
// Feed real Python terminal edits into the browser, including an edit whose
// retained prefix ends halfway through an ANSI sequence before a stderr write.
const consoleResult = spawnSync(process.env.PYTHON || 'python3', ['-c', String.raw`
import json,sys
sys.path.insert(0,sys.argv[1])
from backprop.terminal import Terminal
terminals={name:Terminal() for name in ('stdout','stderr')}
payloads=[]
for stream,text in [
 ('stdout','\x1b[36;1mBACKPROP  /  TRAINING SESSION\x1b[0m\n'),
 ('stdout','Model loaded  ·  124M parameters  ·  accelerator ready\n'),
 ('stdout','\x1b[32m✓\x1b[0m Dataset validated: 18,432 samples\n'),
 ('stdout','\x1b[34;1mEpoch 024/080\x1b[0m  loss=0.1284  accuracy=0.963\n'),
 ('stdout','\x1b[33;3mCheckpoint saved to Foundry\x1b[0m\n'),
 ('stdout','\x1b[31m🙂'),
 ('stderr','\x1b[33;1mwarning\x1b[0m  Evaluation skipped one empty batch.\n'),
 ('stdout','\r\x1b[32m🙂 progress 100%\x1b[0m\n'),
 ('stdout','\x1b[4mvalidation/accuracy\x1b[0m  0.9712   \x1b[32;1mnew best\x1b[0m\n'),
 ('stdout','\x1b[37;44m CHECKPOINT \x1b[0m  immutable snapshot uploaded\n'),
 ('stdout','\x1b[9mretry pending\x1b[29m  recovered successfully\n'),
 ('stdout','\x1b[38;2;116;203;185mRun finished with exit code 0.\x1b[0m\n')]:
 remove,replacement=terminals[stream].feed(text)
 if remove or replacement: payloads.append({'text':replacement,'stream':stream,**({'replace_tail':remove} if remove else {})})
print(json.dumps(payloads))
`, join(root, 'sdk')], { encoding: 'utf8' });
assert.equal(consoleResult.status, 0, `Terminal fixture generation failed: ${consoleResult.stderr}`);
const styledConsole = JSON.parse(consoleResult.stdout);
// Produce manifest-local Table/media references with the actual SDK codec.
// Two versions reuse logical names but retain different immutable contents.
const artifactResult = spawnSync(process.env.PYTHON || 'python3', ['-c', String.raw`
import base64,json,sys,tempfile,zlib,struct,wave
sys.path.insert(0,sys.argv[1])
import backprop as w
def png():
 def chunk(kind,data): return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
 width,height=160,96
 pixels=b''.join(b'\0'+bytes([53,139,128])*width for _ in range(height))
 return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')
sink=w.MemorySink()
with tempfile.TemporaryDirectory() as directory:
 run=w.Run(project='fixture',dir=directory,settings={'console':'off','quiet':True,'summary_warnings':0},_sink=sink,_deployment={})
 for version in ('original','revised'):
  labels=w.Table(columns=['label','description'],data=[['square','Four equal sides'],['circle','Round shape']]); labels.set_pk('label')
  examples=w.Table(columns=['sample_id','label','image','notes'],data=[['sample-a','square',w.Image(png(),caption='Artifact sample A'),'Original square example'],['sample-b','circle',w.Image(png(),caption='Artifact sample B'),version+' circle example <script>window.artifactUnsafe=true</script>']])
  examples.set_pk('sample_id'); examples.set_fk('label',labels,'label')
  predictions=w.Table(columns=['sample_id','example_row','confidence'],data=[['sample-b',examples.index_ref(1),.97],['sample-a',examples.index_ref(0),.86]])
  predictions.set_fk('sample_id',examples,'sample_id')
  artifact=w.Artifact('evaluation-results',type='dataset',description='Saved predictions with related examples, labels and media.')
  artifact.add(predictions,'results/predictions')
  from datetime import datetime,timezone
  import numpy as np
  public=w.Table(columns=['time','array','html'],data=[[datetime(2024,1,1,tzinfo=timezone.utc),np.arange(6).reshape(2,3),w.Html('<p>Public table media</p>',inject=False)]])
  with artifact.new_file('results/public.table.json',mode='w') as stream:json.dump(public.to_json(artifact),stream)
  artifact.add(w.Table(columns=['value'],data=[]),'results/empty')
  artifact.add(w.Table(columns=['nested'],data=[[w.Table(columns=['item','score'],data=[['one',1],['two',2]])]]),'results/nested')
  from backprop.data_types import PartitionedTable
  artifact.add(w.Table(columns=['sample_id','label'],data=[['part-a','square'],['part-b','square']]),'parts/first')
  artifact.add(w.Table(columns=['sample_id','label'],data=[['part-c','circle']]),'parts/second')
  partition=PartitionedTable('parts'); artifact.add(partition,'results/partition')
  artifact.add(w.JoinedTable(partition,labels,'label'),'results/joined')
  run.log_artifact(artifact,aliases=['','v1','<img src=x onerror=window.aliasUnsafe=true>'])
 run.flush()
 run.use_artifact(artifact)
 run.finish()
 auto_sink=w.MemorySink()
 auto=w.Run(project='automatic-table-fixture',entity='demo-user',id='automatic-fixture',dir=directory+'/automatic',settings={'console':'off','quiet':True,'summary_warnings':0},_sink=auto_sink,_deployment={})
 automatic=w.Table(columns=['value'],data=[['automatic history snapshot']],log_mode='MUTABLE')
 auto.log({'automatic_table':automatic})
 automatic.add_data('second automatic row'); auto.log({'automatic_table':automatic})
 auto.summary['automatic_summary']=w.Table(columns=['value'],data=[['automatic summary snapshot']])
 auto.finish()
 summary_run=w.Run(project='summary-viz-fixture',entity='demo-user',id='summary-fixture',tags=['synthetic-demo'],dir=directory+'/summary',settings={'console':'off','quiet':True,'summary_warnings':0},_sink=auto_sink,_deployment={})
 summary_run.summary['final/image']=w.Image(png(),caption='Summary-only image')
 summary_run.summary['final/histogram']=w.Histogram(np_histogram=([1,3,2],[0,1,2,3]))
 summary_run.summary['final/plotly']=w.Plotly({'data':[{'type':'scatter','mode':'lines+markers','name':'estimate','x':[1,10,100],'y':[1,10,100],'error_y':{'type':'data','array':[.2,2,20]}},{'type':'scatter','mode':'markers','name':'hidden reference','visible':'legendonly','x':[1,10,100],'y':[2,20,200]}],'layout':{'title':'Log-scale uncertainty','xaxis':{'type':'log','range':[0,2]},'yaxis':{'type':'log','range':[-1,3]}}})
 summary_run.summary['final/groups']=w.Plotly({'data':[{'type':'bar','name':'control','x':['A','B'],'y':[2,4]},{'type':'bar','name':'experiment','x':['A','B'],'y':[3,6]}],'layout':{'title':'Grouped categorical bars'}})
 summary_run.finish()
 disabled=w.Run(id='disabled-evaluation',entity='demo-user',project='disabled-evaluation-fixture',dir=directory+'/disabled',mode='disabled',_sink=auto_sink,_deployment={},settings={'silent':True,'console':'off'})
 evaluation=w.EvalTable(columns=['answer','score'],data=[['Stored independently',.9]],score_columns=['score'])
 evaluation.bind_to_run(disabled,'evaluation',0);evaluation.to_json(disabled);disabled.finish()
 nested=w.Run(project='nested-media-fixture',entity='demo-user',id='nested-fixture',tags=['synthetic-demo'],dir=directory+'/nested',settings={'console':'off','quiet':True,'summary_warnings':0},_sink=auto_sink,_deployment={})
 nested.define_metric('train.loss',step_metric='train.epoch')
 nested.define_metric('train.epoch',hidden=True)
 table=w.Table(columns=['sample'],data=[],log_mode='INCREMENTAL')
 for step in range(2):
  path=directory+'/audio-'+str(step)+'.wav'
  with wave.open(path,'wb') as audio:
   audio.setparams((1,2,8000,0,'NONE','not compressed')); audio.writeframes(b'\0\0'*8000*(10+step*2))
  table.add_data('nested row '+str(step))
  nested.log({'train':{'loss':.8-step*.4,'epoch':step+1},'train.loss':8-step*4,'outputs':{'table':table,'distribution':w.Histogram(np_histogram=([step+1,step+2],[0,1,2])),'gallery':[w.Image(png(),caption='Nested image')],'audio':w.Audio(path,caption='Real PCM browser fixture')}})
 nested.log_console('Initial nested console\n')
 nested.finish()
 control_sink=w.MemorySink()
 control_run=w.Run(project='control-fixture-source',dir=directory+'/control',settings={'console':'off','quiet':True,'stop_fn':lambda:None},_sink=control_sink,_deployment={})
 control_start=next(event for event in control_sink.events if event['type']=='run_start')
 control_run._stop_checker.trigger(request_id='fixture-request'); control_run.finish()
 control_ack=next(event for event in control_sink.events if event['type']=='run_stop_ack')
print(json.dumps({'control_start':control_start,'control_ack':control_ack,'artifacts':[event['payload'] for event in sink.events if event['type']=='artifact'],'used_artifacts':[event['payload'] for event in sink.events if event['type']=='artifact_use'],'automatic_events':auto_sink.events,'files':{path:base64.b64encode(data).decode() for path,data in {**sink.files,**auto_sink.files}.items()}}))
`, join(root, 'sdk')], { encoding:'utf8' });
assert.equal(artifactResult.status,0,`SDK artifact fixture generation failed: ${artifactResult.stderr}`);
const artifactFixture=JSON.parse(artifactResult.stdout);
// MemorySink omits native dataset routing. Bind its unchanged object bytes to
// the same blobs/ path namespace and dataset descriptor used by FoundryStore.
artifactFixture.files=Object.fromEntries(Object.entries(artifactFixture.files).map(([path,data])=>['blobs/'+path,data]));
for(const artifact of artifactFixture.artifacts) for(const file of artifact.files) { file.dataset_rid=config.media_dataset_rid; file.path='blobs/'+file.path; }
function bindFixtureFiles(value) {
  if(Array.isArray(value)) return value.map(bindFixtureFiles);
  if(!value || typeof value!=='object') return value;
  const result=Object.fromEntries(Object.entries(value).map(([key,item])=>[key,bindFixtureFiles(item)]));
  if(typeof value.path==='string' && ('blobs/'+value.path) in artifactFixture.files) Object.assign(result,{dataset_rid:config.media_dataset_rid,path:'blobs/'+value.path});
  return result;
}
artifactFixture.automatic_events=artifactFixture.automatic_events.map(bindFixtureFiles);
const automaticManifests=[...new Map(artifactFixture.automatic_events.filter(event=>event.project==='automatic-table-fixture').flatMap(event=>event.payload.artifacts || []).map(manifest=>[JSON.stringify([manifest.artifact_id,manifest.version]),manifest])).values()];
assert(automaticManifests.length>=3,'Real Table logging must emit implicit artifact manifests before browser QA');
const automaticHistory=artifactFixture.automatic_events.filter(event=>event.type==='history'&&event.project==='automatic-table-fixture').flatMap(event=>event.payload.artifacts || []);
assert.equal(automaticHistory.length,2,'Each changed logged Table must produce an immutable output');
let sequence = 0;
const fixtureStart = Date.now() - 10 * 60 * 1000;
const event = (run, type, payload) => ({ schema_version: 1, event_id: `test-${sequence++}`, timestamp: new Date(fixtureStart + sequence * 1000).toISOString(), run_id: run, project: 'backprop-demo', entity: 'demo-user', type, payload });
const nativeNestedTable=JSON.parse(await readFile(join(root,'tests/fixtures/native-nested-history.json'),'utf8'));
function fixtures() {
  const result = [];
  for (let n = 0; n < 4; n++) {
    const run = `demo-${n + 1}`;
    result.push(event(run, 'run_start', { name: ['adam-baseline', 'adamw-tuned', 'sgd-momentum', 'lion-exploration'][n], group: n < 2 ? 'adam-family' : 'alternatives', config: { optimizer: ['adam', 'adamw', 'sgd', 'lion'][n], learning_rate: [.003, .001, .01, .0005][n], batch_size: 64, seed: 42, options: [n % 2 ? {b:2,a:1} : {a:1,b:2}], ...(n === 0 ? {nullable:null} : {}), demonstration: true }, tags: ['synthetic-demo', 'classification'] }));
    result.push(event(run, 'metric_definition', { name: 'train/*', step_metric: 'epoch' }));
    for (let step = 0; step < 120; step++) result.push(event(run, 'history', { step, metrics: { epoch: step, 'train/loss': 1.3 * Math.exp(-step / (28 + 10 * n)) + .08 + n * .02 + .035 * Math.sin(step * 2 + n), 'eval/loss': 1.15 * Math.exp(-step / (30 + 8 * n)) + .14 + n * .02, 'eval/accuracy': .55 + .42 * (1 - Math.exp(-step / (25 + 7 * n))), 'train/gradient_norm': .8 * Math.exp(-step / 40) + .14 + .08 * Math.sin(step + n), 'system/throughput': 1400 + n * 100 + 70 * Math.sin(step / 8), 'train/learning_rate': .003 * (1 - step / 130) } }));
    result.push(event(run, 'history', { step: 120, metrics: {
      'samples/predictions': { _type: 'table', columns: ['input', 'predicted', 'actual', 'confidence', 'embedding'], column_types: ['string',{wb_type:'classesId',class_set:[{id:0,name:'<script>window.schemaUnsafe=true</script>'}]},{wb_type:'foreignKey',optional:false,table_id:'ground-truth',column:'label'},'number',{wb_type:'ndarray',shape:[2,2]}], data: [['A red square\n' + 'Long rollout text with tokens and reasoning. '.repeat(25), 'square', 'square', .94], ['A round object', 'circle', 'circle', .91], ['A difficult example', 'circle', 'square', .56]], table_id: `${run}-table`, log_mode: 'MUTABLE', row_offset: 0 },
      'evaluation/confidence': { _type: 'histogram', values: [1, 3, 5, 8, 16, 25, 30, 40], bins: [0, .125, .25, .375, .5, .625, .75, .875, 1] },
      'model/weights': { _type: 'plot', kind: 'bar', table: { columns: ['feature', 'weight'], data: [['length', .8], ['width', -.4], ['angle', .2]] }, fields: { x: 'feature', y: 'weight' }, title: 'Model weights' },
      'evaluation/trajectory': { _type: 'plot', kind: 'line_series', table: { columns: ['epoch', 'value', 'split'], data: [[0, 1, 'train'], [1, .5, 'train'], [2, .2, 'train'], [0, 1.2, 'eval'], [1, .7, 'eval'], [2, .3, 'eval']] }, fields: { x: 'epoch', y: 'value', series: 'split' } },
      'samples/image': { _type: 'image', dataset_rid: config.media_dataset_rid, path: 'blobs/example.png', mime_type: 'image/png', caption: 'Synthetic segmentation fixture', boxes:{prediction:{box_data:[{position:{minX:.2,maxX:.7,minY:.15,maxY:.85},class_id:1,box_caption:'foreground',scores:{confidence:.94}}],class_labels:{'1':'foreground'}}}, masks:{segmentation:{mask:{_type:'image',dataset_rid:config.media_dataset_rid,path:'blobs/mask.png',mime_type:'image/png'},class_labels:{'0':'background','1':'foreground'}}} },
      'evaluation/roc': { _type:'plot',kind:'roc_curve',title:'Receiver operating characteristic',table:{columns:['class','fpr','tpr'],data:[['square',0,0],['square',.1,.75],['square',.5,.95],['square',1,1],['circle',0,0],['circle',.2,.8],['circle',1,1]]},fields:{x:'fpr',y:'tpr',series:'class'},string_fields:{xname:'False positive rate'},split_table:true },
      'evaluation/precision_recall': { _type:'plot',kind:'pr_curve',title:'Precision and recall',table:{columns:['class','recall','precision'],data:[['square',0,1],['square',.5,.98],['square',1,.6]]},fields:{x:'recall',y:'precision',series:'class'} },
      'evaluation/plotly': { _type:'plotly',spec:{data:[{type:'scatter',mode:'lines',x:{dtype:'u1',bdata:'AAEC'},y:{dtype:'u1',bdata:'AwIB'}}],layout:{title:{text:'Typed array Plotly figure'}}} },
      ...(n === 0 ? {'diagnostics/unsupported_plotly':{_type:'plotly',spec:{data:[{type:'scatter',x:[0,1],y:[2,3],yaxis:'y2'}]}}} : {}),
      'diagnostics/nonfinite': {_type:'number',value:'NaN'},
      'notes/html': { _type: 'html', dataset_rid: config.media_dataset_rid, path: 'blobs/example.html', mime_type: 'text/html' },
    } }));
    result.push(...styledConsole.map(payload => event(run, 'console', payload)));
    result.push(event(run, 'console', { text: 'Training complete. <script>window.bad=true</script>', stream: 'stdout' }));
    result.push(event(run, 'run_metadata', {values:{notes:'Updated after initialization. <script>window.bad=true</script>',pinned_config_keys:['missing-key','learning_rate','optimizer']}}));
    result.push(event(run, 'file', {name:'training.txt',policy:'end',file:{dataset_rid:config.media_dataset_rid,path:'blobs/training.txt',mime_type:'text/plain',size:24}}));
    result.push(event(run, 'artifact', {name:'classifier',artifact_type:'model',artifact_id:'artifact-fixture',digest:'0123456789abcdef',version:'v0',state:'COMMITTED',aliases:['latest'],metadata:{accuracy:.95},files:[{name:'weights.bin',dataset_rid:config.media_dataset_rid,path:'blobs/weights.bin',size:128,digest:'abcdef'},{name:'external.table.json',path:'external.table.json',_backprop_external_reference:true,uri:'s3://fixture-bucket/external.table.json',ref:'s3://fixture-bucket/external.table.json',digest:'provider-etag'}]}));
    if(n===0) {
      result.push(...artifactFixture.artifacts.map(artifact=>event(run,'artifact',artifact)));
      result.push(...artifactFixture.used_artifacts.map(item=>event(run,'artifact_use',item)));
      result.push(event(run,'artifact',{name:'invalid-fixture',artifact_type:'dataset',version:'fixture',files:[{name:'broken.table.json',dataset_rid:config.media_dataset_rid,path:'blobs/broken.table.json',mime_type:'application/json'}]}));
    }
    result.push(event(run, 'run_finish', { state: 'finished', exit_code: 0 }));
  }
  result.push({...event('legacy-validation','artifact_metadata',{artifact_id:'legacy-probe',metadata_revision:1,metadata:{probe:true}}),project:'legacy-format-fixture'});
  result.push({...event('other-entity-run','run_start',{name:'Other entity',config:{}}),entity:'other-team',project:'identity-fixture'});
  result.push(...artifactFixture.automatic_events);
  result.push({...event('nested-relations','run_start',{name:'Nested relationships',tags:['synthetic-demo']}),project:'nested-relations-fixture'});
  result.push({...event('nested-relations','history',{step:0,metrics:{nested:nativeNestedTable}}),project:'nested-relations-fixture'});
  for(const id of ['retry','resume','callback']) result.push({...event('control-'+id,'run_start',{name:'Control '+id,tags:['synthetic-demo'],control:{...artifactFixture.control_start.payload.control,instance_id:'instance-'+id}}),project:'stop-control-fixture'});
  return result;
}
let observerReplay=[];
if(process.env.HIGH_COMMAND_UI_FIXTURE){
  const replay=JSON.parse(await readFile(process.env.HIGH_COMMAND_UI_FIXTURE,'utf8'));
  Object.assign(artifactFixture.files,Object.fromEntries(Object.entries(replay.files).map(([path,data])=>['blobs/'+path,data])));
  observerReplay=replay.events.map(bindFixtureFiles).map(record=>({...record,project:'synthetic-project-observer-replay',entity:'demo-user'}));
}
// The shared-writer card receives SDK-produced envelopes with an earlier
// history record delayed after both completion records have become visible.
const exportResult=spawnSync(process.env.PYTHON||'python3',['-B',join(root,'tests/history_export_replay_fixture.py')],{encoding:'utf8'});
assert.equal(exportResult.status,0,`History export fixture failed: ${exportResult.stderr}`);
const exportSnapshots=JSON.parse(exportResult.stdout);
const exportGap=exportSnapshots.find(snapshot=>snapshot.name==='missing_record');
const exportComplete=exportSnapshots.find(snapshot=>snapshot.name==='complete');
const visualizeResult=spawnSync(process.env.PYTHON||'python3',['-B',join(root,'tests/visualize_replay_fixture.py')],{encoding:'utf8'});
assert.equal(visualizeResult.status,0,`Visualize fixture failed: ${visualizeResult.stderr}`);
const visualizeSnapshot=JSON.parse(visualizeResult.stdout).at(-1);
const sklearnSnapshot=sklearnFixture(root,process.env.PYTHON||'python3');
const events = [...fixtures(),...analysisFixtures(event),...navigationFixtures(event),...observerReplay,...exportGap.events,...visualizeSnapshot.events,...(sklearnSnapshot.events||[])];
// Preserve actual typed arrays in table cells, including nested values. The
// UI must expose dimensions immediately while keeping the full data available.
for (const record of events) {
  const table = record.payload.metrics?.['samples/predictions'];
  if (table) {
    table.related_tables={'ground-truth':{_type:'table',table_id:'ground-truth',columns:['label','description'],column_types:[{wb_type:'primaryKey'},'string'],data:[['square','Four equal sides'],['circle','Round shape']],log_mode:'IMMUTABLE'}};
    for (const row of table.data) row.push({_type:'ndarray',dtype:'float32',shape:[2,2],data:[[1,2],[3,4]]});
    if(record.run_id==='demo-1') {
      const snapshot=structuredClone(table);
      snapshot.table_id='full-offline-increment'; snapshot.log_mode='IMMUTABLE'; snapshot.source_log_mode='INCREMENTAL'; snapshot.source_row_offset=80;
      snapshot.data.push(['Recovered example four','square','square',.88,{_type:'ndarray',dtype:'float32',shape:[0],data:[]}],['Recovered example five','circle','circle',.92,{_type:'ndarray',dtype:'float32',shape:[0],data:[]}]);
      Object.assign(table,{legacy_offline:true,nrows:85,log_mode:'INCREMENTAL',artifact_snapshot:snapshot});
    }
  }
}
events.push({...event('nested-fixture','history',{step:2,metrics:{outputs:{video:{_type:'video',dataset_rid:config.media_dataset_rid,path:'blobs/browser.webm',mime_type:'video/webm'},broken_audio:{_type:'audio',dataset_rid:config.media_dataset_rid,path:'blobs/broken.wav',mime_type:'audio/wav'},typed_sequence:{_type:'table',columns:['tuple'],data:[[{_type:'python.sequence',kind:'tuple',data:[1,'two']}]]}}}}),project:'nested-media-fixture',timestamp:new Date().toISOString()});
const records = events.map(({ type, payload, ...rest }) => ({ ...rest, event_type: type, payload: JSON.stringify(payload) }));
const headers = ['schema_version', 'event_id', 'run_id', 'project', 'entity', 'event_type', 'timestamp', 'payload'];
const toCsv = rows => rows.map(row => row.map(value => `"${String(value ?? '').replaceAll('"', '""')}"`).join(',')).join('\n');
let archive = toCsv([headers, ...records.map(record => headers.map(key => record[key]))]);
let failed = false;
let stopWriteMode='success'; const stopWrites=[];
let artifactFailure = false, artifactGate = null;
let emptyWorkspace = false, readGate = null;
const requests = [];
// Small PNG fixtures generated with Node's built-in zlib. No media libraries or
// external files are required; masks encode class IDs as grayscale pixels.
function fixtureImage(mask = false) {
  const width=400,height=240,raw=Buffer.alloc(height*(width*3+1));
  for(let y=0;y<height;y++) for(let x=0;x<width;x++) { const foreground=x>80&&x<280&&y>36&&y<204, i=y*(width*3+1)+1+x*3; const rgb=mask?[foreground?1:0,foreground?1:0,foreground?1:0]:foreground?[85,140+Math.floor(x/12),165]:[21+Math.floor(y/14),30+Math.floor(x/20),33]; rgb.forEach((v,c)=>raw[i+c]=v); }
  const chunk=(name,data)=>{const type=Buffer.from(name), content=Buffer.concat([type,data]);let crc=0xffffffff;for(const byte of content){crc^=byte;for(let i=0;i<8;i++)crc=(crc>>>1)^((crc&1)?0xedb88320:0);}const result=Buffer.alloc(data.length+12);result.writeUInt32BE(data.length);content.copy(result,4);result.writeUInt32BE((crc^0xffffffff)>>>0,data.length+8);return result;};
  const header=Buffer.alloc(13);header.writeUInt32BE(width);header.writeUInt32BE(height,4);header[8]=8;header[9]=2;
  return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',header),chunk('IDAT',deflateSync(raw)),chunk('IEND',Buffer.alloc(0))]);
}
const server = createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    const path = pathname === '/' ? '/index.html' : pathname;
    const data = path === '/index.html' ? releaseHtml : await readFile(join(root, 'web', path));
    response.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[extname(path)] || 'application/octet-stream' }); response.end(data);
  } catch { response.writeHead(404); response.end('Not found'); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await playwright.chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE, headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 1050 }, deviceScaleFactor: 1 });
const page = await context.newPage(), errors = [];
// The installed browser supplies a real VP8 encoder; no downloaded video or
// runtime package is needed to exercise native video playback and seeking.
const videoBytes=Buffer.from(await page.evaluate(async()=>{
 const canvas=document.createElement('canvas');canvas.width=160;canvas.height=96;
 const stream=canvas.captureStream(12), recorder=new MediaRecorder(stream,{mimeType:'video/webm;codecs=vp8'}),chunks=[];
 recorder.ondataavailable=event=>chunks.push(event.data);
 const done=new Promise(resolve=>recorder.onstop=resolve);recorder.start();
 for(let frame=0;frame<12;frame++){const ctx=canvas.getContext('2d');ctx.fillStyle=frame%2?'#358b80':'#b9e778';ctx.fillRect(0,0,160,96);await new Promise(resolve=>setTimeout(resolve,100));}
 recorder.stop();await done;stream.getTracks().forEach(track=>track.stop());
 return Array.from(new Uint8Array(await new Blob(chunks,{type:'video/webm'}).arrayBuffer()));
}));
page.on('pageerror', error => errors.push(error.message));
await context.addInitScript(() => { if (window.top === window && !sessionStorage.getItem('backprop.oauth.session')) sessionStorage.setItem('backprop.oauth.session', JSON.stringify({ accessToken: 'fixture-only-token', expiresAt: Date.now() + 3600000 })); });
await page.route('**/config.json', route => route.fulfill({ json: config }));
await page.route(`${host}/**`, async route => {
  const url = new URL(route.request().url()); requests.push(url.pathname);
  if(url.pathname.endsWith('/authorize')) {
    assert(url.searchParams.get('scope').split(' ').includes('api:use-streams-write'));
    return route.fulfill({status:302,headers:{location:origin+'?code=fixture-code&state='+encodeURIComponent(url.searchParams.get('state'))}});
  }
  if(url.pathname.endsWith('/token')) return route.fulfill({json:{access_token:'fixture-only-token',expires_in:3600,scope:'api:use-streams-read api:use-datasets-read api:use-sql-queries-read api:use-sql-queries-execute api:use-streams-write offline_access'}});
  assert.equal(route.request().headers().authorization, 'Bearer fixture-only-token');
  if(url.pathname.endsWith('/publishRecords')) {
    const body=route.request().postDataJSON(); assert.equal(body.records.length,1);
    const record=body.records[0]; assert.equal(record.project,'stop-control-fixture'); assert.equal(record.event_type,'run_stop_request');
    stopWrites.push(record);
    if(stopWriteMode==='deny') return route.fulfill({status:403,body:'Fixture missing write permission'});
    if(stopWriteMode==='network') return route.abort('failed');
    records.push(record);return route.fulfill({status:204});
  }
  if (failed) return route.fulfill({ status: 503, body: 'Fixture service unavailable' });
  if (readGate) await readGate;
  if (url.pathname.endsWith('/execute')) return route.fulfill({ json: { queryId: 'fixture-query', type: 'succeeded' } });
  if (url.pathname.endsWith('/getResults')) return route.fulfill({ body: emptyWorkspace ? toCsv([headers]) : archive, contentType: 'text/csv' });
  if (url.pathname.endsWith('/getEndOffsets')) return route.fulfill({ json: { '0': String(emptyWorkspace ? 0 : records.length) } });
  if (url.pathname.endsWith('/getRecords')) return route.fulfill({ json: emptyWorkspace ? [] : records.map((value, offset) => ({ value, offset: String(offset) })) });
  if (url.pathname.includes('/files/')) {
    const filePath=decodeURIComponent(url.pathname.split('/files/')[1].replace(/\/content$/,''));
    if(filePath in artifactFixture.files) {
      if(artifactFailure) return route.fulfill({status:503,body:'Fixture artifact temporarily unavailable'});
      if(artifactGate) await artifactGate;
      return route.fulfill({body:Buffer.from(artifactFixture.files[filePath],'base64'),contentType:filePath.endsWith('.png')?'image/png':'application/json'});
    }
    if(filePath==='blobs/browser.webm') return route.fulfill({body:videoBytes,contentType:'video/webm'});
    if(filePath==='blobs/broken.wav') return route.fulfill({body:Buffer.from('invalid PCM file'),contentType:'audio/wav'});
    if(filePath==='blobs/broken.table.json') return route.fulfill({json:{_type:'table',columns:['x'],data:[[1,2]]}});
    if (url.pathname.includes('html')) return route.fulfill({ body: '<!doctype html><html style="background:#16201b;color:#d4ecc4;padding:20px;font:15px sans-serif"><h2>Logged experiment notes</h2><p>Sandboxed HTML fixture.</p><img src="https://tile.openstreetmap.org/forbidden-logged-html.png"><script>parent.window.htmlUnsafe=true</script></html>', contentType: 'text/html' });
    if (url.pathname.includes('training.txt')) return route.fulfill({ body:'Training log fixture\n',contentType:'text/plain' });
    return route.fulfill({ body: fixtureImage(url.pathname.includes('mask.png')), contentType: 'image/png' });
  }
  return route.fulfill({ status: 404, body: 'Unhandled fixture endpoint' });
});
const output = join(root, 'test-results'); await mkdir(output, { recursive: true });
try {
  await page.goto(origin); await page.locator('#app .chart-svg').first().waitFor();
  if(process.env.UI_AUDIT_ONLY==='1') { await verifyNavigationUI({page,output}); } else {
  assert((await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute('content')).includes(`connect-src 'self' blob: ${host}`));
  assert.equal(await page.locator('.run-item').count(), 4);
  assert(!(await page.locator('#app').innerText()).includes('Unsupported event type'));
  await page.locator('.header-project').selectOption('demo-user/legacy-format-fixture');
  assert((await page.locator('#app .notice').innerText()).includes('Unsupported event type: artifact_metadata'));
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');
  assert(!(await page.locator('#app').innerText()).includes('Unsupported event type'));
  assert(await page.getByText('Synthetic demo', { exact: true }).isVisible());
  assert.equal(await page.locator('.chart-card').count(), 7);
  await page.screenshot({ path: join(output, 'workspace-desktop.png'), fullPage: true });
  await page.screenshot({ path: join(output, 'workspace-skin-desktop.png'), fullPage: false });
  await page.setViewportSize({ width: 1300, height: 1000 });
  assert(await page.locator('.panel-legend').evaluateAll(elements => elements.every(element => element.scrollHeight <= element.clientHeight + 1)), 'Four-run legends must remain fully visible');
  await page.screenshot({ path: join(output, 'workspace-1300.png'), fullPage: true });
  await page.setViewportSize({ width: 1600, height: 1050 });
  const chart = page.locator('#app .chart-svg').first(), box = await chart.boundingBox();
  await page.mouse.move(box.x + box.width * .5, box.y + box.height * .5);
  assert(await page.locator('.chart-tooltip').first().isVisible());
  await page.mouse.move(box.x + box.width * .2, box.y + box.height * .5); await page.mouse.down(); await page.mouse.move(box.x + box.width * .6, box.y + box.height * .5); await page.mouse.up();
  assert(await page.locator('.zoom-note').first().isVisible());
  await page.locator('[data-reset-chart]').first().click();
  await page.locator('[data-state="metricSearch"]').fill('loss');
  assert.equal(await page.locator('.chart-card').count(), 2);
  await page.locator('.workspace-settings').evaluate(el=>el.open=true);
  await page.locator('[data-state="xAxis"]').selectOption('epoch');
  await page.locator('[data-state="logScale"]').check();
  await page.locator('[data-state="metricSearch"]').fill('');
  await page.locator('[data-state="logScale"]').uncheck();
  // Per-panel edits have a draft lifecycle and survive reload. Workspace axes
  // remain untouched; legend controls affect only their own panel.
  await page.locator('[data-edit-panel="train/loss"]').click();
  await page.locator('#panel-form [name="title"]').fill('Discard this title');
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  assert(await page.getByText('Discard this title',{exact:true}).isHidden());
  await page.locator('[data-edit-panel="train/loss"]').click();
  await page.locator('#panel-form [name="xMin"]').fill('90');
  await page.locator('#panel-form [name="xMax"]').fill('10');
  assert(await page.getByRole('button',{name:'Apply settings'}).isDisabled());
  await page.locator('#panel-form [name="xMin"]').fill('10');
  await page.locator('#panel-form [name="xMax"]').fill('90');
  await page.locator('#panel-form [name="title"]').fill('Training convergence');
  await page.locator('#panel-form [name="kind"]').selectOption('area');
  await page.locator('#panel-form [name="smoothing"]').fill('0.4');
  await page.locator('#panel-form [name="secondMetric"]').selectOption('eval/loss');
  await page.screenshot({path:join(output,'panel-editor-desktop.png'),fullPage:false});
  await page.getByRole('button',{name:'Apply settings'}).click();
  assert(await page.locator('#app').getByText('Training convergence',{exact:true}).isVisible());
  assert.equal(await page.locator('[data-state="xAxis"]').inputValue(),'epoch');
  const lossPanel=page.locator('[data-metric="train/loss"]');
  assert.equal(await lossPanel.locator('.legend-row').count(),8);
  await lossPanel.locator('.legend-row').first().focus(); await page.keyboard.press('Enter');
  assert(await lossPanel.locator('.legend-row').first().evaluate(node=>node===document.activeElement),'Keyboard legend focus survives rendering');
  assert.equal(await lossPanel.locator('.legend-row').first().getAttribute('aria-pressed'),'false');
  await lossPanel.locator('.legend-row').first().click();
  await page.locator('[data-panel-menu="train/loss"]').click(); await page.getByRole('menuitem',{name:'Pin panel',exact:true}).click();
  assert((await page.locator('.section-toggle').first().innerText()).includes('Pinned'));
  await page.locator('.section-toggle').first().click();
  assert.equal(await page.locator('[data-metric="train/loss"]').count(),0);
  await page.locator('.section-toggle').first().click();
  await page.reload(); await page.locator('#app').getByText('Training convergence',{exact:true}).waitFor();
  await page.locator('[data-edit-panel="train/loss"]').click();
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.screenshot({path:join(output,'panel-editor-mobile.png'),fullPage:false});
  assert(await page.locator('#panel-dialog').evaluate(node=>node.scrollWidth<=node.clientWidth+1));
  assert(await page.getByRole('button',{name:'Apply settings'}).evaluate(node=>node.getBoundingClientRect().bottom<=innerHeight), 'Mobile Apply must remain visible without scrolling');
  await page.locator('#panel-form [name="showLegend"]').focus();
  assert(await page.locator('#panel-form [name="showLegend"]').evaluate(node=>node.getBoundingClientRect().top>0&&node.getBoundingClientRect().bottom<innerHeight), 'Keyboard focus must scroll all mobile settings into view');
  await page.getByRole('button',{name:'Use inherited defaults'}).click();
  await page.setViewportSize({width:1600,height:1050});
  await page.locator('[data-panel-menu="train/loss"]').click(); await page.getByRole('menuitem',{name:'Unpin panel',exact:true}).click();
  await page.locator('#app .chart-svg').first().focus(); await page.keyboard.press('End');
  assert(await page.locator('.chart-tooltip').first().isVisible()); await page.keyboard.press('Escape');
  await page.locator('[data-action="manage-panels"]').click();
  await page.locator('[data-panel-visible="epoch"]').uncheck();
  await page.getByRole('button',{name:'Done',exact:true}).click();
  assert.equal(await page.locator('[data-metric="epoch"]').count(),0);
  await page.locator('[data-view="compare"]').first().click();
  assert.equal((await page.locator('[data-pinned-config]').first().innerText()).trim(), 'learning_rate');
  assert(await page.getByText('learning_rate', { exact: true }).isVisible());
  await page.locator('[data-state="differences"]').check();
  assert.equal(await page.getByText('batch_size', { exact: true }).count(), 0);
  assert.equal(await page.getByText('options', { exact: true }).count(), 0, 'Object insertion order is not a comparison difference');
  const nullable=page.locator('.comparison-table tbody tr').filter({has:page.getByRole('rowheader',{name:'nullable',exact:true})});
  assert.equal(await nullable.getByText('Not logged',{exact:true}).count(),3);
  assert.equal(await nullable.getByText('null',{exact:true}).count(),1);
  await page.locator('[data-state="compareSearch"]').fill('learning_rate');
  assert.equal(await page.locator('.comparison-table tbody th[scope="row"]').count(),1);
  await page.locator('[data-state="compareSearch"]').fill('');
  await page.setViewportSize({width:390,height:844});
  const comparison=page.locator('.comparison-table'); await comparison.focus();
  await page.keyboard.press('ArrowRight'); await page.waitForFunction(()=>document.querySelector('.comparison-table').scrollLeft>0);
  const sticky=await comparison.locator('thead th').first().boundingBox(), region=await comparison.boundingBox();
  assert(Math.abs(sticky.x-region.x)<3,'Parameter labels remain visible while comparison scrolls');
  assert(await page.locator('.comparison-key').evaluateAll(nodes=>nodes.every(node=>node.scrollWidth<=node.clientWidth+1)),'Full parameter names wrap within the sticky column');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),JSON.stringify(await page.evaluate(()=>[...document.querySelectorAll('#app *')].filter(n=>n.getBoundingClientRect().right>innerWidth+2 && n.closest('.comparison-table')===null).slice(0,12).map(n=>[n.className,n.getBoundingClientRect().right]))));
  await page.screenshot({path:join(output,'comparison-mobile.png'),fullPage:false});
  await page.setViewportSize({width:1600,height:1050});
  await page.screenshot({ path: join(output, 'comparison-desktop.png'), fullPage: true });
  await page.locator('.quick-run-filters summary').click();
  await page.locator('[data-state="group"]').selectOption('adam-family');
  await page.locator('[data-view="runs"]').first().click();
  assert.equal(await page.locator('tbody tr').count(),2);
  assert.equal(await page.locator('[data-state="group"]').inputValue(),'adam-family');
  await page.locator('[data-state="group"]').selectOption('');
  assert.equal(await page.locator('tbody tr').count(),4);
  await page.locator('[data-state="search"]').fill('adamw');
  assert.equal(await page.locator('tbody tr').count(), 1);
  await page.locator('[data-state="search"]').fill('');
  await page.locator('[data-action="run-columns"]').click();
  await page.locator('[data-run-column="summary:system/throughput"]').check();
  await page.getByRole('button',{name:'Done',exact:true}).click();
  assert(await page.locator('[data-sort="summary:system/throughput"]').isVisible());
  await page.locator('[data-inspect-run]').first().click();
  assert.equal((await page.locator('#detail-dialog [data-pinned-config]').first().innerText()).trim(), 'learning_rate');
  assert(!(await page.locator('#detail-dialog').innerText()).includes('missing-key'));
  await page.locator('[data-detail-tab="config"]').click();
  assert.equal((await page.locator('#detail-dialog tbody tr').first().locator('td').first().innerText()).trim(), 'learning_rate');
  assert((await page.locator('#detail-dialog').innerText()).includes('batch_size'));
  await page.locator('[data-detail-tab="console"]').click();
  assert((await page.locator('#detail-dialog').innerText()).includes('Training complete'));
  assert.equal(await page.evaluate(() => window.bad), undefined);
  const progress = page.locator('.console-text').filter({hasText:'🙂 progress 100%'});
  assert.equal(await progress.count(),1);
  assert.equal(await progress.locator('span').first().evaluate(element => getComputedStyle(element).color),'rgb(185, 231, 120)');
  assert.equal(await page.locator('.console-text .ansi-bold').first().evaluate(element => getComputedStyle(element).fontWeight),'750');
  assert((await page.locator('.console-text').filter({hasText:'BACKPROP'}).innerText()).includes('TRAINING SESSION'));
  assert.equal(await page.locator('.console-text script,.console-text img,.console-text a').count(),0);
  assert(!(await page.locator('.console-log').innerText()).includes('\x1b'));
  await page.screenshot({path:join(output,'console-styled-desktop.png'),fullPage:false});
  await page.setViewportSize({width:390,height:844});
  assert(await page.locator('.console-log').evaluate(element => element.scrollWidth <= element.clientWidth + 1));
  await page.screenshot({path:join(output,'console-styled-mobile.png'),fullPage:false});
  await page.setViewportSize({width:1600,height:1050});
  await page.locator('[data-detail-tab="files"]').click();
  assert((await page.locator('#detail-dialog').innerText()).includes('training.txt'));
  assert((await page.locator('#detail-dialog').innerText()).includes('COMMITTED'));
  const externalFile=page.locator('#detail-dialog tr').filter({hasText:'external.table.json'});assert.equal(await externalFile.locator('button').count(),0);assert.match(await externalFile.innerText(),/Python SDK with provider credentials/);assert.match(await externalFile.innerText(),/s3:\/\/fixture-bucket/);

  await page.screenshot({path:join(output,'run-files-desktop.png'),fullPage:false});
  await page.locator('#detail-dialog [data-action="close-dialog"]').click();
  await page.locator('[data-inspect-run]').filter({hasText:'adam-baseline'}).first().click();
  await page.locator('[data-detail-tab="files"]').click();
  assert.equal(await page.locator('#detail-dialog .input-artifacts tbody tr').count(),1);
  assert((await page.locator('#detail-dialog .input-artifacts').innerText()).includes('evaluation-results'));
  await page.locator('#detail-dialog .input-artifacts').scrollIntoViewIfNeeded();
  await page.screenshot({path:join(output,'model-input-artifacts.png'),fullPage:false});
  assert.equal(await page.locator('#detail-dialog .artifact-alias[title="Empty alias"]').innerText(), '""');
  assert((await page.locator('#detail-dialog .artifact-alias').allTextContents()).includes('v1'));
  assert.equal(await page.locator('#detail-dialog .artifact-alias img').count(), 0);
  assert.equal(await page.evaluate(() => window.aliasUnsafe), undefined);
  await page.locator('#detail-dialog .artifact-alias[title="Empty alias"]').scrollIntoViewIfNeeded();
  await page.screenshot({path:join(output,'artifact-aliases.png'),fullPage:false});
  const predictionsFiles=page.locator('#detail-dialog tbody tr').filter({has:page.getByText('results/predictions.table.json',{exact:true})});
  assert.equal(await predictionsFiles.count(),2);
  artifactFailure=true;
  await predictionsFiles.first().getByRole('button',{name:'Open table',exact:true}).click();
  await page.locator('#table-dialog .notice.error').waitFor();
  assert.match(await page.locator('#table-dialog .notice.error').innerText(), /503/);
  artifactFailure=false; await page.locator('#table-dialog [data-explorer-retry]').click();
  await page.locator('#table-dialog .logged-table').waitFor();
  assert.equal(await page.locator('#table-dialog tbody tr').count(),2);
  await page.screenshot({path:join(output,'artifact-table-desktop.png'),fullPage:false});
  await page.locator('#table-dialog [data-table-search]').fill('sample-b');
  assert.equal(await page.locator('#table-dialog tbody tr').count(),1);
  await page.locator('#table-dialog [data-table-sort][data-column="2"]').click();
  const artifactCsv=page.waitForEvent('download'); await page.locator('#table-dialog [data-export-table]').click();
  assert((await readFile(await (await artifactCsv).path(),'utf8')).includes('"sample-b","1","0.97"'));
  await page.locator('#table-dialog [data-linked-table][data-column="0"]').focus(); await page.keyboard.press('Enter');
  await page.locator('#table-dialog .row-fields').waitFor();
  assert((await page.locator('#table-dialog').innerText()).includes('original circle example'));
  await page.locator('#table-dialog img.rich-media').waitFor();
  assert.equal(await page.evaluate(()=>window.artifactUnsafe),undefined);
  assert.equal(await page.locator('#table-dialog script').count(),0);
  await page.screenshot({path:join(output,'artifact-linked-row-desktop.png'),fullPage:false});
  await page.locator('#table-dialog [data-linked-table][data-column="1"]').click();
  await page.getByText('Round shape',{exact:true}).waitFor();
  assert.equal(await page.locator('#table-dialog .table-breadcrumbs [aria-current]').count(),1);
  await page.getByRole('button',{name:'Back to previous table',exact:true}).click();
  assert((await page.locator('#table-dialog').innerText()).includes('original circle example'));
  await page.setViewportSize({width:390,height:844});
  assert(await page.locator('#table-dialog').evaluate(node=>node.scrollWidth<=node.clientWidth+1));
  await page.screenshot({path:join(output,'artifact-linked-row-mobile.png'),fullPage:false});
  await page.getByRole('button',{name:'Previous row',exact:true}).click();
  assert((await page.locator('#table-dialog').innerText()).includes('Original square example'));
  await page.getByRole('button',{name:'Back to previous table',exact:true}).click();
  assert.equal(await page.locator('#table-dialog [data-table-search]').inputValue(),'sample-b');
  await page.locator('#table-dialog [data-linked-table][data-column="1"]').click();
  await page.getByText('original circle example <script>window.artifactUnsafe=true</script>',{exact:true}).waitFor();
  await page.getByRole('button',{name:'View all rows',exact:true}).click();
  assert.equal(await page.locator('#table-dialog tbody tr').count(),2);
  await page.locator('#table-dialog [data-table-columns]').click();
  await page.locator('#inspect-dialog [data-column-table][value="3"]').uncheck();
  await page.getByRole('button',{name:'Done',exact:true}).click();
  assert.equal(await page.locator('#table-dialog th').count(),4);
  await page.setViewportSize({width:1600,height:1050});
  await page.keyboard.press('Escape');
  assert(await page.locator('#table-dialog').isHidden());
  assert(await page.locator('#detail-dialog').isVisible());
  // Closing a loading dialog must discard its pending navigation result.
  let releaseArtifact; artifactGate=new Promise(resolve=>releaseArtifact=resolve);
  await predictionsFiles.last().getByRole('button',{name:'Open table',exact:true}).click();
  await page.locator('#table-dialog .loading-dots').waitFor();
  await page.keyboard.press('Escape'); releaseArtifact(); artifactGate=null;
  await page.waitForTimeout(100);
  assert(await page.locator('#table-dialog').isHidden());
  await predictionsFiles.last().getByRole('button',{name:'Open table',exact:true}).click();
  await page.locator('#table-dialog [data-linked-table][data-column="0"]').first().click();
  await page.getByText('revised circle example <script>window.artifactUnsafe=true</script>',{exact:true}).waitFor();
  await page.keyboard.press('Escape');
  const emptyFiles=page.locator('#detail-dialog tbody tr').filter({has:page.getByText('results/empty.table.json',{exact:true})});
  await emptyFiles.first().getByRole('button',{name:'Open table',exact:true}).click();
  await page.locator('#table-dialog .table-pagination').waitFor();
  assert((await page.locator('#table-dialog .table-pagination').innerText()).includes('0-0 of 0'));
  await page.screenshot({path:join(output,'artifact-table-empty.png'),fullPage:false});
  await page.keyboard.press('Escape');
  for(const [name,rows] of [['results/partition.partitioned-table.json',3],['results/joined.joined-table.json',3]]) {
    await page.locator('#detail-dialog tbody tr').filter({has:page.getByText(name,{exact:true})}).first().getByRole('button',{name:'Open table',exact:true}).click();
    await page.locator('#table-dialog .table-pagination').waitFor();
    assert.equal(await page.locator('#table-dialog .logged-table tbody tr').count(),rows);
    assert((await page.locator('#table-dialog').innerText()).includes('part-c'));
    if(name.includes('partition'))assert((await page.locator('#table-dialog .partition-info').innerText()).includes('2 saved parts'));
    await page.screenshot({path:join(output,name.includes('partition')?'partition-table.png':'joined-partition-table.png'),fullPage:false});
    await page.keyboard.press('Escape');
  }
  await page.locator('#detail-dialog tbody tr').filter({has:page.getByText('results/public.table.json',{exact:true})}).first().getByRole('button',{name:'Open table',exact:true}).click();
  await page.locator('#table-dialog .array-cell').waitFor();
  assert.equal(await page.locator('#table-dialog .logged-table tbody tr').count(),1);
  assert((await page.locator('#table-dialog').innerText()).includes('2024-01-01'));
  assert((await page.locator('#table-dialog .array-cell summary').innerText()).includes('2 × 3'));
  await page.locator('#table-dialog .array-cell summary').click();
  assert.deepEqual(JSON.parse(await page.locator('#table-dialog .array-cell pre').innerText()),[[0,1,2],[3,4,5]]);
  await page.frameLocator('#table-dialog iframe').getByText('Public table media',{exact:true}).waitFor();
  await page.screenshot({path:join(output,'public-table-desktop.png'),fullPage:false});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:join(output,'public-table-mobile.png'),fullPage:false});
  await page.setViewportSize({width:1600,height:1050});await page.keyboard.press('Escape');
  const nestedFiles=page.locator('#detail-dialog tbody tr').filter({has:page.getByText('results/nested.table.json',{exact:true})});
  await nestedFiles.first().getByRole('button',{name:'Open table',exact:true}).click();
  const nestedSearch=page.locator('#table-dialog [data-table-search]').last();
  await nestedSearch.waitFor(); await nestedSearch.fill('two');
  assert.equal(await page.locator('#table-dialog .logged-table .logged-table tbody tr').count(),1);
  await page.locator('#table-dialog .logged-table .logged-table [data-inspect-table]').click();
  assert((await page.locator('#table-dialog .row-fields').innerText()).includes('two'));
  await page.getByRole('button',{name:'Back to previous table',exact:true}).click();
  assert.equal(await page.locator('#table-dialog [data-table-search]').last().inputValue(),'two');
  await page.keyboard.press('Escape');
  await page.locator('#detail-dialog tbody tr').filter({hasText:'broken.table.json'}).getByRole('button',{name:'Open table',exact:true}).click();
  await page.locator('#table-dialog .notice.error').waitFor();
  assert((await page.locator('#table-dialog .notice.error').innerText()).includes('valid Table snapshot'));
  await page.screenshot({path:join(output,'artifact-table-error.png'),fullPage:false});
  await page.keyboard.press('Escape');
  await page.locator('#detail-dialog [data-action="close-dialog"]').click();
  await page.locator('.header-project').selectOption('demo-user/automatic-table-fixture');
  await page.locator('[data-inspect-run]').first().click(); await page.locator('[data-detail-tab="files"]').click();
  assert.equal(await page.locator('#detail-dialog .artifact-card').filter({has:page.locator('.artifact-version')}).count(),automaticManifests.length);
  assert((await page.locator('#detail-dialog').innerText()).includes('run_table'));
  for(let index=0;index<automaticHistory.length;index++) {
    const manifest=automaticHistory[index];
    const card=page.locator('#detail-dialog .artifact-card').filter({has:page.locator('.artifact-version').filter({hasText:manifest.version})});
    await card.getByRole('button',{name:'Open table',exact:true}).first().click();
    await page.locator('#table-dialog tbody tr').first().waitFor();
    assert.equal(await page.locator('#table-dialog tbody tr').count(),index+1);
    assert((await page.locator('#table-dialog').innerText()).includes('automatic history snapshot'));
    await page.keyboard.press('Escape');
  }
  const summaryManifest=artifactFixture.automatic_events.find(event=>event.type==='summary'&&event.payload.artifacts?.length).payload.artifacts[0];
  await page.locator('#detail-dialog .artifact-card').filter({has:page.locator('.artifact-version').filter({hasText:summaryManifest.version})}).getByRole('button',{name:'Open table',exact:true}).first().click();
  await page.getByText('automatic summary snapshot',{exact:true}).waitFor();
  await page.screenshot({path:join(output,'automatic-table-artifact.png'),fullPage:false});
  await page.keyboard.press('Escape'); await page.locator('#detail-dialog [data-action="close-dialog"]').click();
  await page.locator('.header-project').selectOption('demo-user/summary-viz-fixture');
  await page.locator('[data-inspect-run]').first().click(); await page.locator('[data-detail-tab="summary"]').click();
  await page.locator('#detail-dialog img.rich-media').waitFor();
  await page.locator('#detail-dialog [data-plotly-ready]').nth(1).waitFor();
  assert.equal(await page.locator('#detail-dialog .plotly-graph').count(),2);
  assert.equal(await page.locator('#detail-dialog .chart-svg').count(),1);
  assert((await page.locator('#detail-dialog').innerText()).includes('Summary-only image'));
  await page.locator('#detail-dialog').getByRole('button',{name:'Show hidden reference',exact:true}).focus(); await page.keyboard.press('Enter');
  assert(await page.locator('#detail-dialog').getByRole('button',{name:'Hide hidden reference',exact:true}).evaluate(node=>node===document.activeElement));
  await page.locator('#detail-dialog').getByRole('button',{name:'Hide hidden reference',exact:true}).click();
  await page.keyboard.press('Escape');
  await page.locator('[data-view="data"]').first().click();
  assert.equal(await page.locator('.rich-card').count(),4);
  assert.equal(await page.locator('[data-rich-step]').count(),0,'Summary-only media must not invent history steps');
  await page.locator('.rich-card img.rich-media').waitFor();
  const logCard=page.locator('.rich-card').filter({has:page.locator('h3').filter({hasText:'final/plotly'})});
  await logCard.locator('[data-plotly-ready]').waitFor();
  assert.equal(await logCard.locator('.point').count(),3);
  assert.equal(await logCard.locator('.yerror').count(),3);
  assert.deepEqual(await logCard.locator('.plotly-graph').evaluate(node=>[node._fullLayout.xaxis.type,node._fullLayout.yaxis.type]),['log','log']);
  await logCard.getByRole('button',{name:'Show hidden reference',exact:true}).focus(); await page.keyboard.press('Enter');
  await page.waitForFunction(()=>[...document.querySelectorAll('.rich-card')].find(n=>n.textContent.includes('final/plotly'))?.querySelectorAll('.point').length===6);
  assert(await logCard.getByRole('button',{name:'Hide hidden reference',exact:true}).evaluate(node=>node===document.activeElement));
  const groups=page.locator('.rich-card').filter({has:page.locator('h3').filter({hasText:'final/groups'})});
  await groups.locator('[data-plotly-ready]').waitFor();
  const barBoxes=await groups.locator('.barlayer .point path').evaluateAll(nodes=>nodes.map(node=>({x:node.getBBox().x,width:node.getBBox().width})));
  assert.equal(barBoxes.length,4); assert(barBoxes[0].x+barBoxes[0].width<=barBoxes[2].x+.1,JSON.stringify(barBoxes));
  await page.locator('#toast').waitFor({state:'hidden'});
  await page.setViewportSize({width:1600,height:1250}); await page.evaluate(()=>scrollTo(0,0));
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  await page.mouse.move(0,0);
  assert(await page.locator('.plotly-container .modebar').evaluateAll(nodes=>nodes.every(node=>getComputedStyle(node).backgroundColor==='rgba(0, 0, 0, 0)')));
  await page.screenshot({path:join(output,'summary-plotly-desktop.png'),fullPage:false});
  await page.setViewportSize({width:390,height:844}); await logCard.scrollIntoViewIfNeeded();
  await page.waitForFunction(()=>[...document.querySelectorAll('#app .plotly-graph')].every(node=>Math.abs(node._fullLayout.width-node.clientWidth)<=1));
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:join(output,'summary-plotly-mobile.png'),fullPage:false});
  await page.setViewportSize({width:1600,height:1050});
  await page.locator('.header-project').selectOption('demo-user/nested-relations-fixture');
  await page.locator('[data-view="data"]').first().click();
  await page.locator('[data-inspect-table]').first().click();
  await page.locator('#inspect-dialog [data-relation-index="0"]').waitFor();
  assert((await page.locator('#inspect-dialog .nested-relations').innerText()).includes('["keys"][0]'));
  await page.screenshot({path:join(output,'nested-relations-desktop.png'),fullPage:false});
  await page.locator('#inspect-dialog [data-relation-index="0"]').focus();await page.keyboard.press('Enter');
  await page.locator('#table-dialog .row-fields').waitFor();
  assert((await page.locator('#table-dialog').innerText()).includes('Row 0'));
  await page.keyboard.press('Escape');await page.keyboard.press('Escape');
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');
  await page.locator('[data-view="data"]').first().click();
  await page.locator('[data-state="richType"]').selectOption('table');
  assert.equal(await page.locator('.rich-card').count(), 4);
  const completeTableDownload=page.waitForEvent('download'); await page.locator('[data-export-rich]').first().click();
  const completeCsv=await readFile(await (await completeTableDownload).path(),'utf8');
  assert(!completeCsv.includes('[object Object]')); assert(completeCsv.includes('ndarray'));
  await page.getByRole('button',{name:'Full artifact snapshot',exact:true}).click();
  await page.locator('#table-dialog tbody tr').first().waitFor();
  assert.equal(await page.locator('#table-dialog tbody tr').count(),5);
  assert((await page.locator('#table-dialog h2').innerText()).includes('this increment'));
  const fullSnapshot=page.waitForEvent('download'); await page.getByRole('button',{name:'Download JSON',exact:true}).click();
  const recovered=JSON.parse(await readFile(await (await fullSnapshot).path(),'utf8'));
  assert.equal(recovered.data.length,5); assert.equal(recovered.row_offset,0);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.rich-card').first().locator('tbody tr').count(),3);
  assert(!(await page.locator('.logged-table').first().innerText()).includes('[object Object]'));
  assert.equal(await page.locator('.array-cell summary').first().innerText(), 'float32 · 2 × 2');
  assert(await page.locator('.array-cell .json-view').first().isHidden());
  await page.locator('[data-state="richRun"]').selectOption('demo-user/backprop-demo/demo-1');
  await page.locator('[data-state="richType"]').selectOption('table');
  await page.locator('[data-run-select="demo-user/backprop-demo/demo-1"]').uncheck();
  assert.match(await page.locator('[data-state="richRun"] option:checked').innerText(),/outside selection/);
  assert.match(await page.locator('[data-state="richType"] option:checked').innerText(),/no matching values/);
  assert.equal(await page.locator('.rich-card').count(),0);
  await page.getByRole('button',{name:'Clear data filters',exact:true}).click();
  assert.equal(await page.locator('[data-state="richRun"]').inputValue(),'');
  assert.equal(await page.locator('[data-state="richType"]').inputValue(),'');
  await page.locator('[data-run-select="demo-user/backprop-demo/demo-1"]').check();
  await page.locator('[data-state="richType"]').selectOption('table');
  await page.locator('.array-cell summary').first().click();
  assert.equal(JSON.parse(await page.locator('.array-cell .json-view').first().innerText())[1][1], 4);
  await page.locator('.array-cell summary').first().click();
  assert.equal(await page.locator('[data-inspect-schema]').first().innerText(), 'Class ID');
  await page.locator('[data-inspect-schema]').first().focus(); await page.keyboard.press('Enter');
  assert((await page.locator('#inspect-dialog .schema-json').innerText()).includes('<script>window.schemaUnsafe=true</script>'));
  assert.equal(await page.evaluate(() => window.schemaUnsafe), undefined);
  await page.screenshot({path:join(output,'table-schema-desktop.png'),fullPage:false});
  await page.keyboard.press('Escape');
  await page.locator('[data-inspect-schema][data-column="2"]').first().click();
  assert((await page.locator('#inspect-dialog .schema-json').innerText()).includes('ground-truth'));
  await page.getByRole('button',{name:'Done',exact:true}).click();
  await page.locator('.rich-card').first().locator('[data-linked-table]').first().click();
  await page.getByText('Four equal sides',{exact:true}).waitFor();
  assert.match(await page.locator('#table-dialog .eyebrow').innerText(), /linked table/i);
  await page.keyboard.press('Escape');
  await page.locator('[data-table-search]').first().fill('difficult');
  assert.equal(await page.locator('.rich-card').first().locator('tbody tr').count(), 1);
  await page.screenshot({ path: join(output, 'tables-desktop.png'), fullPage: true });
  await page.locator('[data-inspect-table]').first().click();
  assert((await page.locator('#inspect-dialog').innerText()).includes('Row 2'));
  await page.locator('#inspect-dialog [data-action="close-dialog"]').click();
  await page.locator('[data-table-search]').first().fill('');
  await page.locator('[data-table-sort][data-column="3"]').first().click();
  assert.equal(await page.locator('.rich-card').first().locator('[data-inspect-table]').first().getAttribute('data-row'),'2');
  await page.locator('[data-table-sort][data-column="3"]').first().click();
  await page.locator('[data-inspect-table]').first().click();
  assert((await page.locator('#inspect-dialog').innerText()).includes('Long rollout text'));
  await page.screenshot({path:join(output,'table-row-desktop.png'),fullPage:false});
  await page.getByRole('button',{name:'Next row',exact:true}).click();
  assert((await page.locator('#inspect-dialog').innerText()).includes('A round object'));
  await page.keyboard.press('Escape');
  await page.locator('[data-table-columns]').first().click();
  await page.locator('[data-column-table][value="2"]').uncheck();
  await page.getByRole('button',{name:'Done',exact:true}).click();
  assert.equal(await page.locator('.rich-card').first().locator('th').count(),5);
  const filteredDownload=page.waitForEvent('download'); await page.locator('[data-export-table]').first().click();
  assert((await readFile(await (await filteredDownload).path(),'utf8')).startsWith('"input","predicted","confidence"'));
  await page.locator('[data-state="richType"]').selectOption('html');
  await page.locator('iframe').first().waitFor();
  await page.frameLocator('iframe').first().getByText('Logged experiment notes', { exact: true }).waitFor();
  assert.equal(await page.locator('iframe').first().getAttribute('sandbox'), '');
  assert((await page.locator('iframe').first().getAttribute('srcdoc')).includes("default-src 'none'"));

  assert.equal(await page.evaluate(() => window.htmlUnsafe), undefined);
  await page.locator('[data-state="richType"]').selectOption('image');
  await page.locator('img.rich-media').first().waitFor();
  await page.locator('canvas.annotation-overlay').first().waitFor();
  assert.equal(await page.locator('.annotation-frame svg rect').count(),4);
  const firstImage=page.locator('.image-viewer').first();
  await firstImage.locator('.annotation-toggle input').first().uncheck();
  assert(await firstImage.locator('svg.annotation-overlay').isHidden());
  await firstImage.locator('.annotation-toggle input').first().check();
  await firstImage.getByRole('slider',{name:'Mask opacity'}).fill('0.65');
  assert.equal(await firstImage.locator('canvas').evaluate(node=>node.style.opacity),'0.65');
  await page.locator('#toast').waitFor({state:'hidden'});
  await page.screenshot({path:join(output,'annotations-desktop.png'),fullPage:true});
  await page.locator('[data-state="richType"]').selectOption('plot');
  await page.locator('[data-state="richSearch"]').fill('roc');
  assert.equal(await page.locator('#app .chart-svg').count(),4);
  await page.locator('.chart-table-disclosure summary').first().click();
  assert.equal(await page.locator('.chart-table-disclosure').first().locator('tbody tr').count(),7);
  await page.screenshot({path:join(output,'roc-desktop.png'),fullPage:true});
  await page.locator('[data-state="richSearch"]').fill('');
  await page.locator('[data-state="richType"]').selectOption('plotly');
  await page.locator('#app [data-plotly-ready]').nth(4).waitFor();
  assert.equal(await page.locator('#app .plotly-graph').count(),5);
  assert.equal(await page.locator('#app .notice.error').count(),0);
  await page.locator('[data-state="richType"]').selectOption('number');
  assert.equal(await page.locator('.nonfinite').count(),4);
  await page.locator('[data-state="richType"]').selectOption('histogram');
  assert.equal(await page.locator('#app .chart-svg').count(), 4);
  await page.locator('[data-view="workspace"]').first().click();
  const before = await page.locator('.chart-card').count(); failed = true;
  await page.locator('[data-action="refresh"]').click();
  await page.locator('#app .notice.error').waitFor();
  assert.equal(await page.locator('.chart-card').count(), before);
  failed = false; await page.locator('[data-action="refresh"]').first().click();
  await page.waitForFunction(() => !document.querySelector('#app .notice.error'));
  await page.locator('[data-view="setup"]').click();
  const downloadPromise = page.waitForEvent('download');
  await page.locator('[data-action="download-config"]').click();
  const downloaded = await downloadPromise;
  const downloadedConfig = JSON.parse(await readFile(await downloaded.path(), 'utf8'));
  assert.equal(downloadedConfig.stream_dataset_rid, config.stream_dataset_rid);
  assert.equal(downloadedConfig.accessToken, undefined); assert.equal(downloadedConfig.client_id, undefined);
  await page.locator('[data-view="workspace"]').first().click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('#toast').waitFor({state:'hidden'});
  await page.evaluate(()=>scrollTo(0,0));
  await page.screenshot({ path: join(output, 'workspace-mobile.png'), fullPage: false });
  await page.locator('#app .chart-card').first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: join(output, 'workspace-mobile-charts.png'), fullPage: false });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), 'Mobile must not overflow horizontally');
  await page.goto(`${origin}/?project=backprop-demo&run=demo-2`); await page.locator('#app .chart-svg').first().waitFor();
  assert.equal(await page.locator('[data-run-select]:checked').count(), 1);
  assert((await page.locator('.chart-subtitle').first().innerText()).startsWith('1 run'));
  await page.goto(`${origin}/?entity=demo-user&project=backprop-demo&run=demo-2&view=data`);
  await page.locator('[data-state="richType"]').waitFor();
  assert.equal(await page.locator('.nav-item[aria-current="page"]').innerText(),'Logged data');
  await page.setViewportSize({width:1600,height:1050});
  await page.locator('.header-project').selectOption('other-team/identity-fixture');
  assert.equal(new URL(page.url()).searchParams.get('entity'),null);
  await page.reload(); await page.locator('.header-project').waitFor();
  await page.waitForFunction(()=>document.querySelector('.project-select')?.value==='other-team/identity-fixture');
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');
  assert.equal(await page.locator('[data-run-select]:checked').count(),1,'Project switching after a deep-link reload preserves the previous project selection');
  await page.locator('body').click({position:{x:4,y:4}}); await page.keyboard.press('1');
  await page.locator('#app .chart-svg').first().waitFor(); await page.keyboard.press('/');
  assert.equal(await page.locator('[data-state="metricSearch"]').evaluate(node=>document.activeElement===node),true);
  // Actual nested SDK rows retain distinct literal keys, history controls and media.
  await page.locator('.header-project').selectOption('demo-user/nested-media-fixture');
  await page.locator('[data-state="metricSearch"]').fill('');
  await page.locator('.workspace-settings').evaluate(el=>el.open=true);
  await page.locator('[data-state="xAxis"]').selectOption('defined');
  assert.equal(await page.locator('.chart-card').count(),2);
  assert(await page.locator('[data-metric="train.loss"]').isVisible());
  assert.equal(await page.locator('.chart-title').filter({hasText:'train\\.loss'}).count(),1);
  assert(await page.getByText(/A backslash escapes a literal dot/).isVisible());
  await page.screenshot({path:join(output,'nested-metrics-desktop.png'),fullPage:false});
  await page.locator('[data-view="data"]').first().click();
  await page.locator('[data-state="richType"]').selectOption('table');
  await page.locator('[data-state="richSearch"]').fill('outputs.table');
  assert.equal(await page.locator('.rich-card tbody tr').count(),2);
  await page.locator('[data-rich-step]').selectOption('0');
  assert.equal(await page.locator('.rich-card tbody tr').count(),1);
  assert(await page.getByText('nested row 0',{exact:true}).isVisible());
  await page.locator('[data-state="richSearch"]').fill('typed_sequence');
  await page.getByText('tuple · 2 items',{exact:true}).click();
  assert((await page.locator('.array-cell pre').innerText()).includes('two'));
  const sequenceDownload=page.waitForEvent('download');await page.locator('[data-export-rich]').click();
  assert((await readFile(await (await sequenceDownload).path(),'utf8')).includes('python.sequence'));
  await page.locator('[data-state="richType"]').selectOption('audio');
  await page.locator('[data-state="richSearch"]').fill('outputs.audio');
  await page.waitForFunction(()=>document.querySelector('audio')?.readyState>=1);
  assert.equal(await page.locator('audio').evaluate(node=>node.duration),12);
  await page.locator('audio').evaluate(async node=>{window.retainedAudio=node;node.currentTime=4;node.volume=.3;node.playbackRate=1.25;await node.play();});
  await page.locator('[data-action="refresh"]').first().click();
  await page.waitForFunction(()=>!document.querySelector('[data-action="refresh"]').disabled&&document.querySelector('audio')?.readyState>=1);
  const preserved=await page.locator('audio').evaluate(node=>({same:node===window.retainedAudio,time:node.currentTime,paused:node.paused,rate:node.playbackRate,volume:node.volume}));
  assert(preserved.same&&!preserved.paused&&preserved.time>=4&&preserved.time<8);assert.equal(preserved.rate,1.25);assert.equal(preserved.volume,.3);
  const rapid=await page.locator('audio').evaluate(async node=>{
    const {replaceView}=await import('/media-view.mjs');
    const root=node.closest('.rich-body'), html=root.innerHTML;
    const loaded=()=>node.readyState>=1?Promise.resolve():new Promise(resolve=>node.addEventListener('loadedmetadata',resolve,{once:true}));
    replaceView(root,html);replaceView(root,html);await loaded();
    const playing=!node.paused, continued=node.currentTime;
    node.pause();node.currentTime=7;
    replaceView(root,html);replaceView(root,html);await loaded();
    const paused=node.paused, saved=node.currentTime;
    node.currentTime=8;node.dispatchEvent(new Event('loadedmetadata'));
    const stale=node.currentTime;
    const scratch=document.createElement('div');document.body.append(scratch);
    scratch.innerHTML='<div data-media-key="duplicate" data-media-ready="true"><span>retained</span></div>';
    replaceView(scratch,'<div data-media-key="duplicate"></div><div data-media-key="duplicate"></div>');
    const once=scratch.children[0].textContent==='retained'&&!scratch.children[1].hasAttribute('data-media-ready');scratch.remove();
    return{playing,continued,paused,saved,stale,once};
  });
  assert(rapid.playing&&rapid.continued>=4&&rapid.continued<8);assert(rapid.paused);assert.equal(rapid.saved,7);assert.equal(rapid.stale,8);assert(rapid.once);
  await page.locator('[data-rich-step]').selectOption('0');
  await page.waitForFunction(()=>document.querySelector('audio')?.readyState>=1&&document.querySelector('audio').duration===10);
  assert.equal(await page.locator('audio').evaluate(node=>node===window.retainedAudio),false,'A different saved snapshot gets its own player');
  assert.equal(await page.locator('audio').evaluate(node=>node.currentTime),0);
  await page.locator('[data-state="richSearch"]').fill('broken_audio');
  await page.locator('.media-decode-error').waitFor();
  const originalDownload=page.waitForEvent('download');await page.getByRole('link',{name:'Download original file'}).click();
  assert.equal(await readFile(await (await originalDownload).path(),'utf8'),'invalid PCM file');
  await page.screenshot({path:join(output,'media-decode-error.png'),fullPage:false});
  await page.locator('[data-state="richType"]').selectOption('video');
  await page.locator('[data-state="richSearch"]').fill('');
  await page.waitForFunction(()=>document.querySelector('video')?.readyState>=2);
  await page.locator('video').evaluate(node=>{window.retainedVideo=node;node.currentTime=.25;node.pause();});
  await page.locator('[data-action="refresh"]').first().click();
  await page.waitForFunction(()=>!document.querySelector('[data-action="refresh"]').disabled&&document.querySelector('video')?.readyState>=2);
  assert(await page.locator('video').evaluate(node=>node===window.retainedVideo&&node.paused&&Math.abs(node.currentTime-.25)<.05));
  await page.locator('video').evaluate(async node=>{node.loop=true;await node.play();});
  await page.locator('[data-action="refresh"]').first().click();
  await page.waitForFunction(()=>!document.querySelector('[data-action="refresh"]').disabled&&document.querySelector('video')?.readyState>=2);
  assert(await page.locator('video').evaluate(node=>node===window.retainedVideo&&!node.paused&&node.currentTime>=0&&node.currentTime<=node.duration&&node.videoWidth===160));
  await page.locator('video').evaluate(node=>node.pause());
  // Long-lived details clearly pause and can read fresh console events on demand.
  await page.locator('[data-inspect-run]').first().click();await page.locator('[data-detail-tab="console"]').click();
  assert(await page.getByText(/Updates paused while inspecting/).isVisible());
  assert((await page.locator('#detail-dialog').innerText()).includes('Initial nested console'));
  const fresh={schema_version:1,event_id:'manual-detail-console',run_id:'nested-fixture',project:'nested-media-fixture',entity:'demo-user',timestamp:new Date(Date.now()+1000).toISOString(),event_type:'console',payload:JSON.stringify({text:'Fresh console after explicit refresh\n',stream:'stdout'})};
  records.push(fresh);
  const beforeRefresh=requests.length;
  await page.getByRole('button',{name:'Refresh view',exact:true}).click();
  await page.getByText('Fresh console after explicit refresh',{exact:false}).waitFor();
  assert(requests.length>beforeRefresh);
  assert(await page.getByRole('button',{name:'Refresh view',exact:true}).evaluate(node=>document.activeElement===node));
  failed=true;await page.getByRole('button',{name:'Refresh view',exact:true}).click();
  await page.locator('#detail-dialog .notice.error').waitFor();
  assert((await page.locator('#detail-dialog').innerText()).includes('Fresh console after explicit refresh'));
  failed=false;await page.getByRole('button',{name:'Refresh view',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#detail-dialog .notice.error')&&!document.querySelector('[data-action="refresh-detail"]').disabled);
  await page.screenshot({path:join(output,'detail-refresh-desktop.png'),fullPage:false});
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(async()=>{window.scrollTo(0,0);await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));});
  assert.equal(await page.locator('.topbar').count(),1);
  await page.screenshot({path:join(output,'detail-refresh-mobile.png'),fullPage:false});
  assert(await page.getByRole('button',{name:'Refresh view',exact:true}).isVisible());
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.keyboard.press('Escape');await page.setViewportSize({width:1600,height:1050});
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');
  await page.locator('.header-project').selectOption('demo-user/disabled-evaluation-fixture');
  await page.locator('[data-view="runs"]').first().click();
  assert.equal(await page.locator('tbody tr').count(),1);
  assert.match(await page.locator('tbody tr').innerText(),/disabled/i);
  await page.locator('[data-inspect-run]').first().click();
  await page.locator('[data-detail-tab="files"]').click();
  assert.equal(await page.locator('.artifact-card').count(),1);
  await page.screenshot({path:join(output,'disabled-evaluation-artifact.png'),fullPage:false});
  await page.keyboard.press('Escape');
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');
  // Stop controls exercise a real OAuth scope upgrade and fixed stream writes
  // entirely against this test-owned host. Nothing targets a live user run.
  const controlEvent=(id,type,payload)=>{const value={...event('control-'+id,type,payload),project:'stop-control-fixture',timestamp:new Date(Date.now()+sequence).toISOString()};records.push({...value,event_type:type,payload:JSON.stringify(payload)});};
  await page.locator('.header-project').selectOption('demo-user/stop-control-fixture');
  await page.locator('[data-inspect-run="demo-user/stop-control-fixture/control-retry"]').first().click();
  await page.getByRole('button',{name:'Stop run',exact:true}).click();
  await page.getByRole('button',{name:'Enable stop controls',exact:true}).waitFor();
  assert.equal(stopWrites.length,0);
  await page.getByRole('button',{name:'Enable stop controls',exact:true}).click();
  await page.getByRole('button',{name:'Send stop request',exact:true}).waitFor();
  assert.equal(stopWrites.length,0,'The OAuth return never sends a stop automatically');
  stopWriteMode='deny';
  await page.getByRole('button',{name:'Send stop request',exact:true}).click();
  await page.locator('#control-dialog .notice.error').waitFor();
  assert.match(await page.locator('#control-dialog .notice.error').innerText(),/write access/);
  stopWriteMode='network';
  await page.getByRole('button',{name:'Send stop request',exact:true}).click();
  await page.getByRole('button',{name:'Retry same request',exact:true}).waitFor();
  const uncertain=stopWrites.at(-1); stopWriteMode='success';
  await page.getByRole('button',{name:'Retry same request',exact:true}).click();
  await page.getByRole('heading',{name:'Stop requested',exact:true}).waitFor();
  assert.deepEqual(stopWrites.at(-1),uncertain,'An uncertain retry retains exact request identity');
  assert.equal(JSON.parse(uncertain.payload).instance_id,'instance-retry');
  await page.screenshot({path:join(output,'run-stop-requested.png'),fullPage:false});
  controlEvent('retry','run_stop_ack',{...artifactFixture.control_ack.payload,instance_id:'instance-retry',request_id:uncertain.event_id});
  await page.locator('#control-dialog').getByRole('button',{name:'Check status',exact:true}).click();
  await page.getByRole('heading',{name:'SDK accepted the request',exact:true}).waitFor();
  assert.match(await page.locator('#control-dialog .control-status').innerText(),/run to report completion/);
  await page.setViewportSize({width:390,height:844});
  assert(await page.locator('#control-dialog').evaluate(node=>node.scrollWidth<=node.clientWidth+1));
  await page.screenshot({path:join(output,'run-stop-mobile.png'),fullPage:false});
  controlEvent('retry','run_finish',{state:'finished',exit_code:0});
  const archivedFinish=records.pop();
  archive=toCsv([headers,...[...records,archivedFinish].map(record=>headers.map(key=>record[key]))]);
  await page.locator('#control-dialog').getByRole('button',{name:'Check status',exact:true}).click();
  await page.getByRole('heading',{name:'Run completed',exact:true}).waitFor();
  await page.keyboard.press('Escape');await page.locator('#detail-dialog [data-action="close-dialog"]').click();
  await page.setViewportSize({width:1600,height:1050});
  await page.locator('[data-inspect-run="demo-user/stop-control-fixture/control-resume"]').first().click();
  await page.getByRole('button',{name:'Stop run',exact:true}).click();
  await page.getByRole('button',{name:'Send stop request',exact:true}).waitFor();
  controlEvent('resume','run_start',{control:{instance_id:'new-resumed-instance',stop_supported:true,poll_interval_seconds:15}});
  const beforeResume=stopWrites.length;
  await page.getByRole('button',{name:'Send stop request',exact:true}).click();
  await page.getByRole('heading',{name:'Run process changed',exact:true}).waitFor();assert.equal(stopWrites.length,beforeResume);
  await page.keyboard.press('Escape');await page.locator('#detail-dialog [data-action="close-dialog"]').click();
  await page.locator('[data-inspect-run="demo-user/stop-control-fixture/control-callback"]').first().click();
  await page.getByRole('button',{name:'Stop run',exact:true}).click();
  await page.getByRole('button',{name:'Send stop request',exact:true}).click();
  await page.getByRole('heading',{name:'Stop requested',exact:true}).waitFor();
  controlEvent('callback','run_stop_ack',{instance_id:'instance-callback',request_id:stopWrites.at(-1).event_id,status:'failed',error:'ValueError <img src=x onerror=window.controlUnsafe=true>'});
  await page.locator('#control-dialog').getByRole('button',{name:'Check status',exact:true}).click();
  await page.getByRole('heading',{name:'Stop callback failed',exact:true}).waitFor();
  assert.equal(await page.locator('#control-dialog img').count(),0);assert.equal(await page.evaluate(()=>window.controlUnsafe),undefined);
  assert.equal(await page.getByRole('button',{name:'Send stop request',exact:true}).count(),0);
  await page.keyboard.press('Escape');await page.locator('#detail-dialog [data-action="close-dialog"]').click();
  await page.locator('[data-inspect-run="demo-user/stop-control-fixture/control-resume"]').first().click();
  let releaseControlRead; readGate=new Promise(resolve=>releaseControlRead=resolve);
  const beforeCancel=stopWrites.length;
  await page.getByRole('button',{name:'Stop run',exact:true}).click();
  await page.getByRole('heading',{name:'Checking the latest run',exact:true}).waitFor();
  await page.keyboard.press('Escape');releaseControlRead();readGate=null;
  assert.equal(await page.locator('#control-dialog').evaluate(node=>node.open),false);
  assert.equal(stopWrites.length,beforeCancel,'Closing a pending status read never publishes a stop');
  await page.locator('#detail-dialog [data-action="close-dialog"]').click();
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');

  await verifyNavigationUI({page,output});
  await verifyAnalysisUI({page,output,observerReplay:observerReplay.length>0,appendHistory:payload=>{const {type,...record}={...event('hc-ui-0','history',payload),project:'synthetic-project-ui-fixture'};records.push({...record,event_type:type,payload:JSON.stringify(payload)});archive=toCsv([headers,...records.map(record=>headers.map(key=>record[key]))]);}});
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');

  await page.locator('.header-project').selectOption('e/history-export-replay');
  await page.locator('[data-view="runs"]').first().click();
  await page.locator('[data-inspect-run="e/history-export-replay/shared-export"]').first().click();
  const writerCard=page.locator('#detail-dialog .artifact-card').filter({has:page.getByRole('heading',{name:'Shared writers',exact:true})});
  assert.match(await writerCard.innerText(),/rank1[\s\S]*finished[\s\S]*1 record not visible yet/);
  assert.equal(await writerCard.getByText('Complete',{exact:true}).count(),1);
  await writerCard.scrollIntoViewIfNeeded();
  await page.screenshot({path:join(output,'history-completeness-desktop.png'),fullPage:false});
  await page.setViewportSize({width:390,height:844});
  await writerCard.scrollIntoViewIfNeeded();
  assert(await page.locator('#detail-dialog').evaluate(node=>node.scrollWidth<=node.clientWidth+1));
  await page.screenshot({path:join(output,'history-completeness-mobile.png'),fullPage:false});
  await page.setViewportSize({width:1600,height:1050});
  const visibleExportIds=new Set(exportGap.events.map(event=>event.event_id));
  for(const {type,payload,...rest} of exportComplete.events.filter(event=>!visibleExportIds.has(event.event_id)))
    records.push({...rest,event_type:type,payload:JSON.stringify(payload)});
  archive=toCsv([headers,...records.map(record=>headers.map(key=>record[key]))]);
  await page.getByRole('button',{name:'Refresh view',exact:true}).click();
  await writerCard.getByText('1 record not visible yet',{exact:true}).waitFor({state:'detached'});
  assert.equal(await writerCard.getByText('Complete',{exact:true}).count(),2);
  await page.keyboard.press('Escape');
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');

  await verifySklearnUI(page,sklearnSnapshot,output);
  await page.locator('.header-project').selectOption('e/visualize-replay');
  await page.locator('[data-view="data"]').first().click();
  await page.locator('[data-state="richType"]').selectOption('');
  await page.locator('[data-state="richSearch"]').fill('');
  await page.locator('[data-state="richRun"]').selectOption('');
  const importanceCard=page.locator('.rich-card').filter({has:page.getByRole('heading',{name:'importance',exact:true})});
  const registeredCard=page.locator('.rich-card').filter({has:page.getByRole('heading',{name:'registered',exact:true})});
  await importanceCard.locator('[data-vega-ready="true"]').waitFor();
  await registeredCard.locator('[data-vega-ready="true"]').waitFor();
  assert((await importanceCard.locator('[data-vega-ready="true"] svg').textContent()).includes('embedding'));
  assert(await importanceCard.locator('[data-vega-ready="true"] svg path').count()>0);
  const confusionCard=page.locator('.rich-card').filter({has:page.getByRole('heading',{name:'confusion',exact:true})});
  assert((await confusionCard.innerText()).includes('positive'));
  await importanceCard.getByRole('combobox',{name:'History step for importance',exact:true}).selectOption('0');
  await importanceCard.locator('[data-vega-ready="true"]').waitFor();
  assert(!(await importanceCard.locator('[data-vega-ready="true"] svg').textContent()).includes('embedding'));
  await importanceCard.getByRole('combobox',{name:'History step for importance',exact:true}).selectOption('');
  await importanceCard.locator('[data-vega-ready="true"]').waitFor();
  await page.screenshot({path:join(output,'visualize-desktop.png'),fullPage:false});
  await page.setViewportSize({width:390,height:844});
  await importanceCard.scrollIntoViewIfNeeded();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.screenshot({path:join(output,'visualize-mobile.png'),fullPage:false});
  await page.setViewportSize({width:1600,height:1050});
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');

  // Render loading and empty states through the same real API boundary. A
  // deliberate response gate makes this deterministic without timed sleeps.
  await page.setViewportSize({width:1600,height:1050});
  let releaseReads; readGate=new Promise(resolve=>releaseReads=resolve);
  await page.goto(`${origin}/?view=workspace`); await page.getByRole('heading',{name:'Opening your project'}).waitFor();
  assert(await page.locator('.header-project').isDisabled());
  assert.equal(await page.locator('[data-action=refresh]').count(),0);
  await page.screenshot({path:join(output,'workspace-loading.png'),fullPage:false});
  readGate=null; releaseReads(); await page.locator('#app .chart-svg').first().waitFor();
  emptyWorkspace=true; await page.reload();
  await page.getByText('Your next experiment starts here.',{exact:true}).waitFor();
  await page.screenshot({path:join(output,'workspace-empty.png'),fullPage:false});
  await page.getByRole('button',{name:'Connect your code',exact:true}).last().click();
  assert(await page.getByRole('button',{name:'Download connection file'}).isVisible());
  const signedOut=await browser.newContext({viewport:{width:1300,height:900}}), authPage=await signedOut.newPage();
  await authPage.route('**/config.json',route=>route.fulfill({json:config}));
  await authPage.goto(origin); await authPage.getByRole('button',{name:'Continue with Foundry'}).waitFor();
  await authPage.screenshot({path:join(output,'workspace-signed-out.png'),fullPage:false});
  await signedOut.close();
  }
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: true, browser: 'isolated headless Chromium-family process', api: 'real modules with fixture REST responses', screenshot_directory: output, runtime_errors: errors.length, rest_requests: requests.length }));
} catch(error) {await page.screenshot({path:join(output,'ui-failure.png'),fullPage:false});await writeFile(join(output,'ui-failure.txt'),await page.locator('body').innerText());throw error;} finally { await browser.close(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
