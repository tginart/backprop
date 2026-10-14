/**
 * Architecture: High Command-shaped synthetic envelopes -> existing real
 * Foundry REST fixture -> browser workflows -> saved screenshots/assertions.
 * Main caller: browser_qa.mjs. The metric names and declared rollout axis come
 * from direct inspection of demo-user/synthetic-project in W&B. Values are generated,
 * clearly tagged synthetic-demo, and never published to live Foundry datasets.
 * No browser cookies or user profile are read by this isolated fixture.
 */
import assert from 'node:assert/strict';
import {join} from 'node:path';
export function analysisFixtures(event) {
  const events=[],add=(run,type,payload)=>events.push({...event(run,type,payload),project:'synthetic-project-ui-fixture'});
  for(let seed=0;seed<4;seed++){
    const id=`hc-ui-${seed}`;
    add(id,'run_start',{name:`hc-rollout-seed-${seed}`,group:seed<2?'baseline':'candidate',tags:['synthetic-demo','synthetic-project','train'],config:{seed,learning_rate:seed<2?.00001:.00002,training:{batch_size:4},model:'Qwen3-8B',nullable:seed===0?null:0}});
    add(id,'metric_definition',{name:'rollout/*',step_metric:'train/rollout'});
    add(id,'metric_definition',{name:'train/rollout',hidden:true});
    for(let step=0;step<96-seed*5;step++){
      if(seed===1 && step%7===0)continue;
      const rollout=step*4,metrics={'train/rollout':rollout,'rollout/reward':.15+.7*(1-Math.exp(-step/(28+seed*3)))+.03*Math.sin(step/2+seed),'rollout/judge_score':.35+.5*(1-Math.exp(-step/35))+seed*.01,'rollout/replay_mean_abs_nats':.8*Math.exp(-step/23)+.025*(seed+1),'rollout/replay_p95_abs_nats':1.5*Math.exp(-step/20)+.04*(seed+1),'system/gpu_memory_gb':43+seed+.8*Math.sin(step/8)};
      if(step===32)metrics['rollout/reward']={_type:'number',value:'NaN'};
      add(id,'history',{step,metrics});
    }
    add(id,'summary',{values:{final_reward:.82+seed*.01,zero:0,...(seed===0?{nullable:null}:{})}});
    add(id,'console',{text:`Rollout evaluation completed for seed ${seed}.\n`,stream:'stdout'});
    add(id,'run_finish',{state:'finished',exit_code:0});
  }
  return events;
}

/** Exercise user actions rather than directly assigning internal app state.
 * Main callers: browser_qa.mjs after rich-media/control regressions complete.
 * Local storage is inspected only to verify that user actions were persisted.
 */
export async function verifyAnalysisUI({page,output,appendHistory,observerReplay=false}) {
  const project='demo-user/synthetic-project-ui-fixture';
  await page.locator('.header-project').selectOption(project);
  await page.locator('[data-view="workspace"]').first().click();
  await page.locator('.workspace-settings').evaluate(el=>el.open=true);
  await page.locator('[data-state="columns"]').selectOption('2');
  assert.equal(await page.locator('.run-item').count(),4);
  assert.equal(await page.locator('.chart-card').count(),5);
  assert.match(await page.locator('.chart-card[data-metric="rollout/reward"] .chart-footer').innerText(),/train\/rollout/);
  await page.screenshot({path:join(output,'synthetic-project-workspace-before-layout.png'),fullPage:false});
  // Typed config + summary filters share the same table and chart selection.
  await page.locator('[data-action="run-filters"]').click();
  await page.getByRole('button',{name:'+ Add condition',exact:true}).click();
  await page.getByLabel('Filter field 1',{exact:true}).selectOption('config:learning_rate');
  await page.getByLabel('Filter comparison 1',{exact:true}).selectOption('eq');
  await page.getByLabel('Filter value 1',{exact:true}).fill('0.00001');
  await page.getByRole('button',{name:'+ Add condition',exact:true}).click();
  await page.getByLabel('Filter field 2',{exact:true}).selectOption('summary:zero');
  await page.getByLabel('Filter value 2',{exact:true}).fill('0');
  assert.match(await page.locator('[data-filter-preview]').innerText(),/2 of 4/);
  await page.getByRole('button',{name:'Apply filters',exact:true}).click();
  assert.equal(await page.locator('.run-item').count(),2);
  await page.locator('[data-view="runs"]').first().click();
  assert.equal(await page.locator('tbody tr').count(),2);
  await page.locator('[data-action="run-columns"]').click();
  await page.locator('[data-run-column="config:training.batch_size"]').check();
  await page.getByRole('button',{name:'Done',exact:true}).click();
  assert(await page.locator('[data-sort="config:training.batch_size"]').isVisible());
  await page.locator('[data-sort="run:name"]').click();
  assert.match(await page.locator('tbody tr').first().innerText(),/seed-0/);
  await page.screenshot({path:join(output,'synthetic-project-run-table.png'),fullPage:false});
  await page.locator('[data-action="run-filters"]').click();await page.getByRole('button',{name:'Clear conditions',exact:true}).click();await page.getByRole('button',{name:'Apply filters',exact:true}).click();
  await page.locator('[data-view="workspace"]').first().click();
  await page.locator('[data-action="run-grouping"]').click();
  await page.getByLabel('Group by',{exact:true}).selectOption('config:learning_rate');
  await page.getByLabel('Variation band',{exact:true}).selectOption('minmax');
  await page.getByRole('button',{name:'Done',exact:true}).click();
  assert.equal(await page.locator('[data-metric="rollout/reward"] .legend-row').count(),2);
  assert.equal(await page.locator('[data-metric="rollout/reward"] .variation-band').count(),4,'Each group has two ribbon segments around the explicit NaN gap');
  await page.screenshot({path:join(output,'synthetic-project-grouped-workspace.png'),fullPage:false});
  const zoomCard=page.locator('[data-metric="rollout/reward"]'), svgBox=await zoomCard.locator('.chart-svg').boundingBox();
  const ticksBefore=await zoomCard.locator('.axis').textContent();
  await page.mouse.move(svgBox.x+svgBox.width*.32,svgBox.y+svgBox.height*.30);await page.mouse.down();await page.mouse.move(svgBox.x+svgBox.width*.72,svgBox.y+svgBox.height*.68);await page.mouse.up();
  assert(await zoomCard.locator('[data-reset-chart]').isVisible());
  assert.notEqual(await zoomCard.locator('.axis').textContent(),ticksBefore);
  await page.screenshot({path:join(output,'synthetic-project-chart-zoom.png'),fullPage:false});
  await zoomCard.locator('.chart-svg').focus();await page.keyboard.press('0');assert.equal(await zoomCard.locator('[data-reset-chart]').count(),0);

  // Sidebar resizing is pointer based with the same keyboard-accessible handle.
  const handle=page.getByRole('separator',{name:'Resize run sidebar'});await handle.focus();await page.keyboard.press('ArrowRight');
  assert.equal(await handle.getAttribute('aria-valuenow'),'266');
  const bounds=await handle.boundingBox();await page.mouse.move(bounds.x+3,bounds.y+50);await page.mouse.down();await page.mouse.move(bounds.x+83,bounds.y+50);await page.mouse.up();
  assert.equal(await handle.getAttribute('aria-valuenow'),'346');
  await page.locator('[data-action="add-section"]').click();
  await page.getByLabel('Section name',{exact:true}).fill('Rollout review');
  await page.getByLabel('Default smoothing',{exact:true}).fill('.4');
  await page.getByRole('button',{name:'Apply section',exact:true}).click();
  const section=page.locator('.metric-section').filter({has:page.getByRole('button',{name:/Rollout review.*0 panels/})});
  const sectionId=await section.getAttribute('data-section-id');assert(sectionId);
  await page.locator('[data-panel-menu="rollout/reward"]').click();await page.getByRole('menuitem',{name:'Move / resize panel',exact:true}).click();
  await page.getByLabel('Section',{exact:true}).selectOption(sectionId);
  await page.getByLabel('Panel width',{exact:true}).selectOption('2');
  await page.getByLabel('Panel height',{exact:true}).fill('380');
  await page.getByRole('button',{name:'Apply layout',exact:true}).click();
  const card=()=>page.locator('.chart-card[data-metric="rollout/reward"]');
  assert.equal(await card().evaluate(node=>node.closest('[data-section-id]').dataset.sectionId),sectionId);
  assert.equal(Math.round((await card().boundingBox()).height),380);
  await page.locator('[data-edit-panel="rollout/reward"]').click();
  assert.equal(await page.locator('#panel-dialog [name="smoothing"]').inputValue(),'0.4');
  await page.locator('#panel-dialog [name="title"]').fill('Rollout reward');
  await page.getByRole('button',{name:'Apply settings',exact:true}).click();
  await page.getByRole('button',{name:'Settings for Rollout review section',exact:true}).click();
  await page.getByLabel('Default smoothing',{exact:true}).fill('.6');
  await page.getByRole('button',{name:'Apply section',exact:true}).click();
  await page.locator('[data-edit-panel="rollout/reward"]').click();assert.equal(await page.locator('#panel-dialog [name="smoothing"]').inputValue(),'0.6','Changing only a panel title leaves smoothing inherited');await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await page.locator('[data-panel-drag="rollout/judge_score"]').dragTo(page.locator(`[data-section-id="${sectionId}"] .section-heading-row`));
  assert.equal(await page.locator('[data-metric="rollout/judge_score"]').evaluate(node=>node.closest('[data-section-id]').dataset.sectionId),sectionId);
  const resize=card().locator('[data-panel-resize]');await resize.focus();await page.keyboard.press('ArrowDown');assert.equal(Math.round((await card().boundingBox()).height),400);
  await resize.scrollIntoViewIfNeeded();const grip=await resize.boundingBox();
  await page.mouse.move(grip.x+3,grip.y+3);await page.mouse.down();await page.mouse.move(grip.x+3,grip.y-57);await page.mouse.up();
  assert.equal(Math.round((await card().boundingBox()).height),340);
  await resize.focus();for(let step=0;step<3;step++)await page.keyboard.press('ArrowDown');
  assert.equal(Math.round((await card().boundingBox()).height),400);
  const expandedIds=await page.locator('[data-section][aria-expanded="true"]').evaluateAll(nodes=>nodes.map(node=>node.dataset.section));
  for(const id of expandedIds)await page.locator(`[data-section="${id}"]`).click();
  await page.locator('[data-section-drag="system"]').dragTo(page.locator(`[data-section-id="${sectionId}"] .section-heading-row`));
  const ids=await page.locator('[data-section-id]').evaluateAll(nodes=>nodes.map(node=>node.dataset.sectionId));
  assert(ids.indexOf('system')<ids.indexOf(sectionId),JSON.stringify(ids));
  for(const id of expandedIds)await page.locator(`[data-section="${id}"]`).click();

  await page.locator('[data-run-color="demo-user/synthetic-project-ui-fixture/hc-ui-0"]').fill('#609cff');
  await page.locator('[data-action="save-view-as"]').click();await page.getByLabel('View name',{exact:true}).fill('Rollout comparison');await page.getByRole('button',{name:'Save view',exact:true}).click();
  const viewId=await page.getByLabel('Saved workspace view',{exact:true}).inputValue();assert(viewId);
  await page.locator('[data-state="search"]').fill('seed');
  await page.locator('[data-action="save-view"]').click();
  await page.reload();await card().waitFor();
  assert.equal(await page.getByLabel('Saved workspace view',{exact:true}).inputValue(),viewId);
  assert.equal(await page.locator('[data-state="search"]').inputValue(),'seed');
  assert.equal(await page.getByRole('separator',{name:'Resize run sidebar'}).getAttribute('aria-valuenow'),'346');
  assert.equal(Math.round((await card().boundingBox()).height),400);
  assert.equal(await page.locator('[data-run-color="demo-user/synthetic-project-ui-fixture/hc-ui-0"]').inputValue(),'#609cff');
  const selectedBefore=await page.locator('[data-run-select]:checked').count();
  appendHistory({step:97,metrics:{'train/rollout':388,'rollout/reward':.93}});
  await page.getByRole('button',{name:'Refresh',exact:true}).click();
  assert.equal(await page.locator('[data-run-select]:checked').count(),selectedBefore);
  assert.equal(await page.getByLabel('Saved workspace view',{exact:true}).inputValue(),viewId);
  assert.equal(Math.round((await card().boundingBox()).height),400);
  await card().locator('.chart-svg').focus();await page.keyboard.press('Home');
  assert.match(await card().locator('.chart-tooltip').innerText(),/runs.*σ/s);
  await page.keyboard.press('Escape');
  await page.screenshot({path:join(output,'synthetic-project-saved-layout.png'),fullPage:false});
  await page.getByLabel('Saved workspace view',{exact:true}).selectOption('');
  await page.locator('.workspace-settings').evaluate(el=>el.open=true);
  await page.locator('[data-state="columns"]').selectOption('1');
  await page.getByLabel('Saved workspace view',{exact:true}).selectOption(viewId);
  assert.equal(await page.locator('[data-state="columns"]').inputValue(),'2');
  // Dedicated pages ignore project filters and keep stable bookmarks and tabs.
  await page.locator('[data-run="demo-user/synthetic-project-ui-fixture/hc-ui-0"]').click();await page.locator('.run-page').waitFor();
  assert.equal(await page.locator('.run-page .chart-card').count(),5);
  for(const tab of ['overview','config','summary','console','files','artifacts']){await page.locator(`[data-run-tab="${tab}"]`).click();assert.match(page.url(),new RegExp('tab='+tab));assert.equal(await page.locator('[data-run-tab][aria-current="page"]').getAttribute('data-run-tab'),tab);}
  await page.locator('[data-run-tab="console"]').click();assert.match(await page.locator('.console-log').innerText(),/seed 0/);
  await page.reload();await page.locator('.console-log').waitFor();assert.match(await page.locator('.console-log').innerText(),/seed 0/);
  await page.locator('[data-run-tab="charts"]').click();await page.screenshot({path:join(output,'synthetic-project-run-page.png'),fullPage:false});
  await page.getByRole('button',{name:'Next run',exact:true}).click();assert.match(await page.locator('.run-page h1').innerText(),/seed-1/);
  await page.goBack();assert.match(await page.locator('.run-page h1').innerText(),/seed-0/);
  await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:join(output,'synthetic-project-run-mobile.png'),fullPage:false});
  await page.locator('[data-view="workspace"]').first().click();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:join(output,'synthetic-project-workspace-mobile.png'),fullPage:false});
  await page.setViewportSize({width:1600,height:1050});
  // Preference failures are visible and cannot claim that a view was saved.
  const priorView=await page.getByLabel('Saved workspace view',{exact:true}).inputValue();
  await page.evaluate(()=>{window.originalStorageSet=Storage.prototype.setItem;Storage.prototype.setItem=function(key,value){if(key==='backprop.workspace.v1')throw new DOMException('Storage full','QuotaExceededError');return window.originalStorageSet.call(this,key,value);};});
  await page.locator('[data-action="save-view-as"]').click();await page.getByLabel('View name',{exact:true}).fill('Unstored view');await page.getByRole('button',{name:'Save view',exact:true}).click();
  assert.match(await page.locator('#inspect-dialog [role="alert"]').innerText(),/storage is unavailable/);
  assert.equal(await page.getByLabel('Saved workspace view',{exact:true}).inputValue(),priorView);
  await page.getByRole('button',{name:'Cancel',exact:true}).click();
  await page.evaluate(()=>{Storage.prototype.setItem=window.originalStorageSet;delete window.originalStorageSet;});
  await page.locator('.workspace-settings').evaluate(el=>el.open=true);
  await page.locator('[data-state="columns"]').selectOption('2');
  if(observerReplay){
    await page.locator('.header-project').selectOption('demo-user/synthetic-project-observer-replay');
    await page.locator('.chart-svg').first().waitFor();
    assert.equal(await page.locator('.run-item').count(),1);
    await page.locator('[data-state="metricSearch"]').fill('rollout/');
    assert(await page.locator('.chart-card').count()>0);
    await page.locator('#toast').waitFor({state:'hidden'});
    await page.screenshot({path:join(output,'synthetic-project-real-observer-replay.png'),fullPage:false});
    await page.locator('[data-run]').first().click();
    await page.locator('[data-run-tab="summary"]').click();
    const committed=page.locator('.kv-table tbody tr').filter({has:page.getByText('overview/committed_groups',{exact:true})});
    assert.match(await committed.innerText(),/28/,'Resumed observer summary retains the real committed-group count');
    await page.locator('[data-run-tab="charts"]').click();assert(await page.locator('.chart-svg').count()>40);
    await page.locator('[data-view="workspace"]').first().click();
    await page.locator('[data-state="metricSearch"]').fill('');
  }
}
