/** Genuine Audio/Video descriptors agree across SDK and browser replay. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {normalizeEvents} from '../web/model.mjs';

test('audio/video metadata and bytes survive duplicate/reordered events',()=>{
 const fixture=JSON.parse(execFileSync(process.env.BACKPROP_PYTHON||'python3',['-B','tests/audio_video_replay_fixture.py'],{encoding:'utf8',env:{...process.env,PYTHONPATH:'sdk'}}));
 const view=normalizeEvents([...fixture.events].reverse().concat(fixture.events));assert.deepEqual(view.issues,[]);const run=view.runs[0];
 for(const key of ['config','summary','state'])assert.deepEqual(JSON.parse(JSON.stringify(run[key])),fixture.expected[key]);
 const rows=run.history.map(row=>Object.fromEntries(Object.entries(row).filter(([key])=>!['_timestamp','_runtime'].includes(key))));assert.deepEqual(rows,fixture.history);
 assert.equal(rows[0].audio._type,'audio');assert.equal(rows[0].audio.sample_rate,8000);assert.equal(rows[0].audio.duration,3/8000);assert.equal(rows[0].audio.caption,'clip');
 assert.equal(rows[0].video._type,'video');assert.equal(rows[0].video.format,'gif');assert.equal(rows[0].video.caption,'animation');
 assert.equal(Buffer.from(fixture.files.audio,'base64').subarray(0,4).toString(),'RIFF');assert.equal(Buffer.from(fixture.files.video,'base64').subarray(0,6).toString(),'GIF89a');
});
