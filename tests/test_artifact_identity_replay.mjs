/** Public Python artifact identities agree with actual browser event reads. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';
test('artifact references and commit identity agree after edits and duplicate replay',()=>{
 const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/artifact_identity_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));
 assert.deepEqual(view.issues,[]);assert.equal(view.runs.length,1);
 const run=view.runs[0];assert.deepEqual(JSON.parse(JSON.stringify(run.config)),fixture.config);
 const artifact=run.artifacts.find(value=>value.artifact_id===fixture.reference.id);
 assert(artifact);assert.equal(artifact.digest,fixture.commit);
 assert.equal(artifact.version,fixture.reference.version);
 assert.equal(artifact.name,fixture.reference.sequenceName);
 assert.equal(artifact.description,'updated description');
});
