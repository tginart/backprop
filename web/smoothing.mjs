/**
 * Architecture: immutable recorded points -> continuous segments -> exponential
 * weights -> displayed points retaining raw values and recorded uncertainty.
 * Main callers: model.seriesFor and grouped metric panels. A recorded gap resets
 * the accumulator; smoothing never manufactures a missing metric or X value.
 * W&B's published EMA/TWEMA recurrences include a debias denominator. TWEMA
 * normalizes X spacing to the full segment range. Our explicit viewport scale
 * is 1000; W&B's documentation does not specify its production scale constant.
 */
export function smoothSeries(points, {smoothing = 0, smoothingMethod = 'ema', viewportScale = 1000} = {}) {
  if (!Number.isFinite(smoothing) || smoothing < 0 || smoothing >= 1) throw new Error('Smoothing must be between zero and one.');
  if (!['ema','twema'].includes(smoothingMethod)) throw new Error('Unknown smoothing method.');
  if (!smoothing) return points.map(point=>({...point,raw:point.raw ?? point.y}));
  const segments=[];
  for(const point of points){if(!segments.length || point.breakBefore)segments.push([]);segments.at(-1).push(point);}
  return segments.flatMap(segment=>{
    let sum=0,weight=0;
    const range=segment.at(-1).x-segment[0].x, base=smoothingMethod==='twema'?Math.min(Math.sqrt(smoothing),.999):smoothing;
    return segment.map((point,index)=>{
      const dx=index?point.x-segment[index-1].x:0;
      const decay=smoothingMethod==='twema' && range>0 ? base ** (dx/range*viewportScale) : base;
      sum=sum*decay+point.y;weight=weight*decay+1;
      return {...point,raw:point.raw ?? point.y,y:sum/weight};
    });
  });
}
