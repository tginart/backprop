/**
 * Architecture: captured mutation boundaries -> direct held geometry at common
 * normalized times -> unchanged geometry budget. Historical RAF/resampling
 * helpers remain for recorded-CI regressions, including rejected old evidence.
 * Main callers: browser easing comparison and focused recorded-CI regressions.
 * Why this exists: SVG updates are discrete. A complete geometry-change observer
 * gives the actual held value; an invented curve between drawings can disagree
 * even when both actual charts are within the native comparison contract.
 */
import assert from 'node:assert/strict';

/** Validate the actual drawing sequence without inventing a curve.
 * Main callers: historical bucket construction and live held-state comparison.
 * Multiple ordered drawings may share a rounded clock value; a backwards clock
 * or nonfinite value is always invalid. Query-time ambiguity is checked separately.
 */
function orderedObservations(record,project) {
 const samples=record.samples.map(sample=>({time:sample.time,value:project(sample.value)}));
 for(const [index,sample] of samples.entries()){
  assert(Number.isFinite(sample.time)&&Number.isFinite(sample.value),'finite captured animation observation');
  if(index)assert(sample.time>=samples[index-1].time,'nondecreasing captured animation times');
 }
 return samples;
}

/** Preserve observations when a rounded browser clock repeats a timestamp.
 * Main caller: recordedProgress. Each bucket keeps every observed value; its
 * midpoint and half-range express the ambiguity, rather than choosing a sample
 * or pretending two drawings have different known times. A backwards clock or
 * excessive observed spread remains a failure, including outside query times.
 */
function observationBuckets(record,project,maxUncertainty) {
 const buckets=[];
 for(const sample of orderedObservations(record,project)) {
  const value=sample.value,last=buckets.at(-1);
  if(last?.time===sample.time)last.observations.push(value);
  else buckets.push({time:sample.time,observations:[value]});
 }
 return buckets.map(bucket=>{
  const minimum=Math.min(...bucket.observations),maximum=Math.max(...bucket.observations),uncertainty=(maximum-minimum)/2;
  assert(uncertainty<=maxUncertainty,'equal-time animation geometry spread exceeds observation budget');
  return{...bucket,value:minimum+uncertainty,uncertainty};
 });
}

/** Read measured geometry known to be unchanged while an observer watched SVG.
 * Main callers: recordedProgress for a genuine plateau, compareHeldProgress
 * for instantaneous modules. Verify both recorded drawing boundaries. A sparse
 * RAF record has no such evidence and remains an explicit observation failure.
 * Other path/style properties can change while this selected geometry holds;
 * these intervals describe the measured point/number/bounds, not every pixel.
 */
function capturedHeldProgress(record,time,project) {
 if(record.observation!=='svg-mutation')return null;
 const interval=record.held_intervals.find(item=>item.start<=time&&item.end>time);
 if(!interval)return null;
 const index=record.samples.findIndex(sample=>sample.time===interval.start&&JSON.stringify(sample.value)===JSON.stringify(interval.value));
 assert(index>=0&&index+1<record.samples.length,'held interval has captured boundary drawings');
 assert.equal(record.samples[index+1].time,interval.end,'held interval ends at the next observed drawing');
 return{time,value:project(interval.value),uncertainty:0,held_interval:interval,method:'direct unchanged SVG geometry'};
}

/** Estimate captured scalar geometry at one shared animation time.
 * Main caller: compareRecordedProgress. Require nearby real observations and
 * neighboring slopes; do not extrapolate or invent missing samples. The local
 * curvature estimate includes clock-bin envelopes as well as linear resampling
 * uncertainty. This is an empirical observation bound, not a proof about
 * unobserved arbitrary curve behavior.
 */
export function recordedProgress(record,time,project,{maxGap=.04,maxUncertainty=.04}={}) {
 const samples=observationBuckets(record,project,maxUncertainty);
 const index=samples.findIndex(sample=>sample.time>=time);
 assert(index>=0,'no animation observation extrapolation');
 if(samples[index].time===time)return{time,value:samples[index].value,uncertainty:samples[index].uncertainty,bracket:[samples[index]],neighbors:[]};
 if(index<2||index+1>=samples.length||samples[index].time-samples[index-1].time>maxGap){
  const held=capturedHeldProgress(record,time,project);if(held)return held;
 }
 assert(index>=2&&index+1<samples.length,'surrounding animation observations required');
 const [previous,lower,upper,next]=samples.slice(index-2,index+2),width=upper.time-lower.time;
 assert(width<=maxGap,'animation observation gap exceeds bounded resampling window');
 const slopes=[(lower.value-previous.value)/(lower.time-previous.time),(upper.value-lower.value)/width,(next.value-upper.value)/(next.time-upper.time)];
 const slopeUncertainty=[(lower.uncertainty+previous.uncertainty)/(lower.time-previous.time),(upper.uncertainty+lower.uncertainty)/width,(next.uncertainty+upper.uncertainty)/(next.time-upper.time)];
 const curvature=Math.max((Math.abs(slopes[1]-slopes[0])+slopeUncertainty[1]+slopeUncertainty[0])/((upper.time-previous.time)/2),(Math.abs(slopes[2]-slopes[1])+slopeUncertainty[2]+slopeUncertainty[1])/((next.time-lower.time)/2));
 const fraction=(time-lower.time)/width,clockUncertainty=(1-fraction)*lower.uncertainty+fraction*upper.uncertainty;
 const uncertainty=curvature*width*width/8+clockUncertainty;
 assert(uncertainty<=maxUncertainty,'animation resampling uncertainty exceeds observation budget');
 return{time,value:lower.value+(upper.value-lower.value)*fraction,uncertainty,clock_uncertainty:clockUncertainty,bracket:[lower,upper],neighbors:[previous,next],observed_curvature:curvature};
}

/** Compare geometry at common quarter/half/three-quarter normalized times.
 * Main caller: historical recorded-CI regressions. Include both uncertainties in
 * the existing0.12 budget, rather than adding a tolerance for clock mismatch.
 * Exact final geometry is checked separately before this helper is called.
 */
export function compareRecordedProgress(viewer,reference,project,{duration=900}={}) {
 return [.25,.5,.75].map(time=>{
  const options={maxGap:36/duration,maxUncertainty:.04},actual=recordedProgress(viewer,time,project,options),expected=recordedProgress(reference,time,project,options);
  const difference=Math.abs(actual.value-expected.value),boundedDifference=difference+actual.uncertainty+expected.uncertainty;
  assert(boundedDifference<.12,'matched-time animation geometry: '+JSON.stringify({time,difference,boundedDifference,actual,expected}));
  return{time,viewer_time:time,reference_time:time,viewer:actual.value,reference:expected.value,difference,bounded_difference:boundedDifference,viewer_observation:actual,reference_observation:expected};
 });
}

/** Compare directly observed held geometry for every native SVG family.
 * Main caller: live browser easing cases, including instantaneous modules.
 * Each interval comes from a live geometry-changing mutation observer;
 * selected measured geometry held between its recorded boundaries. This does
 * not interpolate through the long interval or claim continuous animation.
 */
export function compareHeldProgress(viewer,reference,project) {
 return [.25,.5,.75].map(time=>{
  const observations=[viewer,reference].map(record=>{
   const samples=orderedObservations(record,project);
   assert.equal(record.observation,'svg-mutation','held state requires a live drawing observer');
   const observed=capturedHeldProgress(record,time,project);
   assert(observed,'captured held-state interval must surround the requested time');
   // A full instantaneous step in an earlier clock tick does not make a later
   // known held interval ambiguous. Only drawings sharing the queried timestamp
   // can make that exact boundary uncertain; keep their existing0.04 cap.
   const atBoundary=samples.filter(sample=>sample.time===time);
   const ambiguity=atBoundary.length?(Math.max(...atBoundary.map(sample=>sample.value))-Math.min(...atBoundary.map(sample=>sample.value)))/2:0;
   assert(ambiguity<=.04,'queried equal-time animation geometry spread exceeds observation budget');
   return{...observed,uncertainty:ambiguity};
  });
  const [actual,expected]=observations,difference=Math.abs(actual.value-expected.value),boundedDifference=difference+actual.uncertainty+expected.uncertainty;
  assert(boundedDifference<.12,'matched-time held animation geometry');
  return{time,viewer_time:time,reference_time:time,viewer:actual.value,reference:expected.value,difference,bounded_difference:boundedDifference,viewer_observation:actual,reference_observation:expected};
 });
}
