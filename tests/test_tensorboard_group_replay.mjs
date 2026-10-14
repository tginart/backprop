/** Actual independent Python journals -> browser history, summaries and writers.
 * Why this exists: grouping is a persistence rule, so a renderer-only example
 * cannot prove source ordering, late observations or duplicate summary behavior.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
import {semanticValue} from '../web/tensorboard-replay.mjs';

const fixture = JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3', ['-B','-c', `
import json
from tensorboard_grouping_replay_fixture import fixtures
from backprop.apis.public.replay import reduce_events
result = fixtures()
for case in result['cases'].values():
    if not case['expect_conflict']:
        state = reduce_events(case['events'])
        case['python_history'] = state['history']
        case['python_summary'] = state['summary']
print(json.dumps(result))
`], {encoding:'utf8', maxBuffer:32*1024*1024, env:{...process.env,PYTHONPATH:'sdk:tests'}}));

const visible = row => semanticValue(Object.fromEntries(Object.entries(row)
  .filter(([key])=>!key.startsWith('_') || ['_timestamp','_tensorboard_incomplete'].includes(key))));
const physical = row => Object.fromEntries(['_step','_writer','_writer_label'].filter(key=>key in row).map(key=>[key,row[key]]));
for (const [name, sample] of Object.entries(fixture.cases)) {
  test(`grouped TensorBoard ${name}: source order, summary and duplicate delivery`, () => {
    for(const events of [sample.events,[...sample.events].reverse(),[...sample.events].reverse().concat(sample.events)]) {
      if(sample.expect_conflict) {assert.throws(()=>normalizeEvents(events),/Conflicting TensorBoard/);continue;}
      const model=normalizeEvents(events),run=model.runs.find(item=>item.id==='group-replay');
      assert.equal(model.runs.length,1);
      assert.equal(Object.keys(run.writers).length,sample.expected_writer_count);
      assert.deepEqual(run.history.map(visible),sample.expected_history);
      assert.deepEqual(run.history.map(visible),sample.python_history.map(visible));
      assert.deepEqual(run.history.map(physical),sample.python_history.map(physical));
      assert.deepEqual(visible(run.summary),sample.expected_summary);
      assert.deepEqual(visible(run.summary),visible(sample.python_summary));
      assert.equal(model.issues.some(item=>item.includes('source observations are incomplete')),
        sample.expected_history.some(row=>row._tensorboard_incomplete));
    }
  });
}

const interactions = JSON.parse(execFileSync(process.env.BACKPROP_PYTHON || 'python3', ['-B','-c',
  'import json; from tensorboard_group_interactions import fixtures; print(json.dumps(fixtures()))'],
  {encoding:'utf8',maxBuffer:32*1024*1024,env:{...process.env,PYTHONPATH:'sdk:tests'}}));
const flatten = (values,prefix='') => Object.fromEntries(Object.entries(values).flatMap(([key,value])=>{
  if(key.startsWith('_')) return [];
  const name=prefix+key;
  if(value && typeof value==='object' && !Array.isArray(value) && !value._type) return Object.entries(flatten(value,name+'.'));
  return [[name,value]];
}));
for(const [name,sample] of Object.entries(interactions)) test(`TensorBoard ${name} matches logging and SDK replay`,()=>{
  for(const events of [sample.events,[...sample.events].reverse(),sample.events.concat(sample.events)]) {
    const run=normalizeEvents(events).runs[0];
    assert.deepEqual(run.history.map(row=>flatten(row)),sample.expected_history);
    for(const [key,value] of Object.entries(sample.expected_summary)) assert.deepEqual(run.summary[key],value);
  }
});
