/**
 * Architecture: native SVG mutations -> geometry-changing draw observations
 * -> renderer Date.now frame origin -> held measured geometry and cleanup.
 * Main caller: plotly_animation_browser_qa interpolation cases.
 * Why this exists: an unrelated RAF poll can observe an older drawing after its
 * native transition timer has advanced. Microtask mutation delivery observes
 * completed SVG updates and keeps the renderer and observer in the same clock.
 * This observes DOM updates; it never replaces clocks or renderer functions.
 */

/** Watch actual drawings until each native frame queue completes.
 * Main caller: the browser easing comparison. Keep geometry changes, clock
 * readings and unchanged-geometry intervals, then disconnect every observer on
 * success or failure. A missing shape remains a failure rather than a sample.
 */
export function observePlotlySvg(nodes,shape,duration) {
 const records=nodes.map(node=>({before:shape(node),samples:[],mutation_flushes:[],held_intervals:[],start:null,done:false,observation:'svg-mutation',clock:'native Date.now'}));
 const last=records.map(record=>({value:record.before,time:0}));
 let resolve,reject,remaining=nodes.length;
 const complete=new Promise((yes,no)=>{resolve=yes;reject=no;});
 const observers=[];
 const disconnect=()=>{observers.forEach(observer=>observer.disconnect());records.forEach(record=>{record.observer_disconnected=true;});};
 const fail=error=>{clearTimeout(deadline);disconnect();reject(error);};
 const capture=(node,index,phase,mutations=0)=>{
  const record=records[index],dateBefore=Date.now(),performanceBefore=performance.now(),value=shape(node),dateAfter=Date.now(),performanceAfter=performance.now();
  const time=(dateAfter-record.start)/duration,changed=JSON.stringify(value)!==JSON.stringify(last[index].value);
  const capture={date_before:dateBefore,date_after:dateAfter,performance_before:performanceBefore,performance_after:performanceAfter};
  if(phase==='mutation')record.mutation_flushes.push({time,value,mutations,changed,capture});
  if(phase==='mutation'&&!changed)return;
  if(record.samples.length)record.held_intervals.push({start:last[index].time,end:time,value:last[index].value,ended_by:phase});
  record.samples.push({time,value,phase,mutations,capture});
  last[index]={time,value};
 };
 const deadline=setTimeout(()=>fail(Error('Native animation controls did not complete.')),duration+10000);
 nodes.forEach((node,index)=>{
  const record=records[index],observer=new MutationObserver(mutations=>{
   if(record.start===null||record.done)return;
   try{capture(node,index,'mutation',mutations.length);}catch(error){fail(error);}
  });
  observer.observe(node,{subtree:true,attributes:true,childList:true,characterData:true});observers.push(observer);
  node.once('plotly_animatingframe',event=>{
   record.start=node._transitionData._lastFrameAt;
   record.start_performance=performance.now();
   record.options={frame:event.animation.frame,transition:event.animation.transition};
   try{if(!Number.isFinite(record.start))throw Error('No native frame clock origin.');capture(node,index,'frame-start');}catch(error){fail(error);}
  });
  node.once('plotly_animated',()=>{
   try{capture(node,index,'queue-end');record.done=true;record.queue_end={value:last[index].value,work:window.plotlyDrawingWork(node),time:Date.now()-record.start};
    if(--remaining===0){clearTimeout(deadline);disconnect();resolve();}
   }catch(error){fail(error);}
  });
 });
 return{records,complete};
}
