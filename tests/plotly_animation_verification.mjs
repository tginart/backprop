/**
 * Architecture: strict self-comparison validates both complete drawing records
 * -> unchanged paired 0.12 comparison -> retain every quarter-time outcome.
 * Main callers: explicit browser animation QA and recorded-CI unit regressions.
 * Why this exists: separate native transitions do not share an exact start or
 * draw phase. A valid geometry-budget mismatch leaves timing parity unverified;
 * malformed observations and every other runtime assertion must still fail.
 */
import assert from 'node:assert/strict';
import {compareHeldProgress} from './plotly_animation_sampling.mjs';

/** Retain a timing mismatch without turning it into a successful comparison.
 * Main caller: browser interpolation after its portable behavior assertions.
 * Validate all three query boundaries on both records before attempting the
 * paired comparison. This prevents an early geometry mismatch from concealing
 * a later missing drawing, invalid clock or ambiguous observation. Only the
 * exact geometry-budget assertion is diagnostic; other errors propagate.
 */
export function retainIntermediateTiming(viewer,reference,project) {
 const [actual,expected]=[viewer,reference].map(record=>compareHeldProgress(record,record,project));
 const sampled=actual.map((sample,index)=>{
  const observed=sample.viewer_observation,native=expected[index].viewer_observation;
  const difference=Math.abs(observed.value-native.value),boundedDifference=difference+observed.uncertainty+native.uncertainty;
  return{time:sample.time,viewer_time:sample.time,reference_time:sample.time,viewer:observed.value,reference:native.value,difference,bounded_difference:boundedDifference,viewer_observation:observed,reference_observation:native,geometry_budget_met:boundedDifference<.12};
 });
 let rejection=null;
 try {
  compareHeldProgress(viewer,reference,project);
 } catch(error) {
  if(!(error instanceof assert.AssertionError&&error.message==='matched-time held animation geometry'))throw error;
  assert(sampled.some(sample=>!sample.geometry_budget_met),'strict geometry rejection must have a retained mismatched sample');
  rejection={message:error.message,code:error.code,operator:error.operator};
 }
 return{status:'partial/unverified',sample_budget_outcome:rejection?'geometry_budget_mismatch':'captured_samples_within_budget',geometry_budget:.12,comparison:'direct held SVG at independently normalized quarter times',sampled,failures:sampled.filter(sample=>!sample.geometry_budget_met),strict_rejection:rejection};
}

/** Expose the unverified claim separately from successful runtime assertions.
 * Main callers: successful and failed browser artifact summaries. A run with no
 * captured mismatch still does not establish universal intermediate-time parity.
 * Keep each named mismatch and all its measured values available for review.
 */
export function summarizeIntermediateTiming(curves) {
 const namedFailures=[];
 let withinBudget=0,sampleCount=0;
 for(const curve of curves) {
  const timing=curve.intermediate_timing;
  assert.equal(timing?.status,'partial/unverified','each captured curve retains its timing verification status');
  assert.equal(timing.sampled.length,3,'each captured curve retains all quarter-time comparisons');
  sampleCount+=timing.sampled.length;
  if(timing.failures.length)namedFailures.push({name:curve.name,start_offset_ms:curve.start_offset_ms,comparison:curve.comparison,strict_rejection:timing.strict_rejection,samples:timing.failures});
  else withinBudget++;
 }
 return{status:'partial/unverified',claim:'Portable intermediate-time geometry parity is not established by separately started native transitions.',comparison:'direct held SVG at independently normalized quarter times',geometry_budget:.12,captured_case_count:curves.length,captured_sample_count:sampleCount,captured_cases_within_budget:withinBudget,geometry_budget_mismatch_case_count:namedFailures.length,geometry_budget_mismatch_sample_count:namedFailures.reduce((count,row)=>count+row.samples.length,0),named_failures:namedFailures};
}
