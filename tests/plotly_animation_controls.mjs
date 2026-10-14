/**
 * Architecture: one real viewer pointer click -> native SVG control handlers
 * -> paired command/event evidence in one task, or deliberate staggered clicks.
 * Main caller: plotly_animation_browser_qa easing comparisons and diagnostics.
 * Why this exists: separate Playwright clicks can start identical native charts
 * hundreds of milliseconds apart on unrelated drawing grids. The reference
 * control can run in the viewer click's task without changing renderer clocks.
 */

/** Observe both native controls and optionally dispatch the reference together.
 * Main caller: interpolation's browser setup before its real pointer action.
 * The viewer retains trusted pointer input. The reference receives an explicit
 * native SVG click; its command and Plotly event must still be observed. Neither
 * renderer.animate nor any clock is replaced or called directly by this helper.
 */
export function armNativePlotlyControls(nodes,{sameTask=false}={}) {
 const buttons=nodes.map(node=>node.querySelector('g.updatemenu-button'));
 if(buttons.some(button=>!button))throw Error('Missing actual native animation control.');
 const records=nodes.map(()=>({pointer:[],commands:[]}));
 nodes.forEach((node,index)=>{
  buttons[index].addEventListener('click',event=>records[index].pointer.push({time:Date.now(),performance:performance.now(),trusted:event.isTrusted,target:event.target.tagName}),{capture:true,once:true});
  node.once('plotly_buttonclicked',event=>records[index].commands.push({time:Date.now(),performance:performance.now(),label:event.button.label,method:event.button.method,args:structuredClone(event.button.args),execute:event.button.execute}));
 });
 if(sameTask)buttons[0].addEventListener('click',()=>buttons[1].dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window})),{once:true});
 return{mode:sameTask?'same-task':'staggered',records};
}
