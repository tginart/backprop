/**
 * Architecture: synthetic runs -> real app -> user actions -> saved state and
 * screenshots. Main caller: browser_qa.mjs. Checks the October W&B audit at
 * interaction level, including keyboard focus and narrow-screen clipping.
 */
import assert from 'node:assert/strict';
import {join} from 'node:path';
export function navigationFixtures(event) {
  const events=[];
  for(let index=0;index<25;index++) {
    const add=(type,payload)=>events.push({...event(`nav-${index}`,type,payload),project:'navigation-ui-fixture'});
    add('run_start',{name:`experiment-${String(index).padStart(2,'0')}`,group:index%2?'candidate':'baseline',tags:['synthetic-demo'],config:{learning_rate:.01+index*.001,batch_size:32},notes:'Synthetic navigation test'});
    for(let step=0;step<3;step++)add('history',{step,metrics:Object.fromEntries(Array.from({length:14},(_,metric)=>[`train/metric_${String(metric).padStart(2,'0')}`,step+index+metric]))});
    add('summary',{values:{score:index/25}});add('run_finish',{state:'finished',exit_code:0});
  }
  return events;
}
export async function verifyNavigationUI({page,output}) {
  await page.setViewportSize({width:1600,height:1050});
  await page.locator('.header-project').selectOption('demo-user/navigation-ui-fixture');
  await page.locator('.nav-item[data-view="workspace"]').click();
  assert.equal(await page.locator('.project-tabs').count(),0,'One project navigation hierarchy');
  assert.equal(await page.locator('.main h1').textContent(),'Workspace');
  const nav=page.locator('.nav-item[data-view="workspace"]'), tip=page.locator('#control-tooltip');
  const before=page.url();await nav.hover();await tip.waitFor({state:'visible'});
  assert.equal(await tip.textContent(),'Workspace');assert.equal(page.url(),before);
  const labelBox=await tip.boundingBox(), navBox=await nav.boundingBox();assert(labelBox.x>navBox.x+navBox.width,'Rail label is outside the rail');
  await page.screenshot({path:join(output,'navigation-hover.png')});
  await page.mouse.move(900,80);await tip.waitFor({state:'hidden'});
  await nav.focus();await tip.waitFor({state:'visible'});await page.keyboard.press('Escape');await tip.waitFor({state:'hidden'});
  await page.keyboard.press('Tab');await tip.waitFor({state:'visible'});assert.equal(await tip.textContent(),'Runs');
  await page.keyboard.press('Escape');

  await page.locator('[data-action="run-columns"]').click();
  const shown=page.locator('[data-run-column="config:learning_rate"]'), pin=page.locator('[data-pin-column="config:learning_rate"]');
  await shown.check();await pin.check();await page.getByRole('button',{name:'Done',exact:true}).click();
  assert.equal(await page.locator('[data-pinned-field="config:learning_rate"]').count(),20);
  await page.locator('.run-pin').first().click();
  const pinnedName=await page.locator('.run-name').first().textContent();
  await page.locator('[data-state="search"]').fill('experiment-2');
  await page.locator('[data-run-select]').first().check();
  const chosen=await page.locator('[data-run-select]:checked').count();assert(chosen>0);
  await page.locator('[data-run-color]').first().fill('#b788eb');
  const saved=()=>page.evaluate(()=>JSON.parse(localStorage.getItem('backprop.workspace.v1')));
  const snapshot=await saved();
  await page.getByRole('button',{name:'Hide runs sidebar',exact:true}).click();assert.equal(await page.locator('.run-panel').count(),0);
  await page.getByRole('button',{name:'Show runs',exact:true}).click();
  assert.equal(await page.locator('[data-state="search"]').inputValue(),'experiment-2');assert.equal(await page.locator('[data-run-select]:checked').count(),chosen);
  await page.getByRole('button',{name:'Expand full runs table',exact:true}).click();
  assert(await page.locator('[data-sort="config:learning_rate"]').isVisible());
  assert.equal(await page.locator('[data-state="search"]').inputValue(),'experiment-2');
  await page.getByRole('button',{name:'Collapse runs to sidebar',exact:true}).click();
  const after=await saved();assert.deepEqual(after.selected,snapshot.selected);assert.deepEqual(after.runColors,snapshot.runColors);assert.deepEqual(after.analysisPreferences,snapshot.analysisPreferences);
  await page.locator('[data-state="search"]').fill('');assert.equal(await page.locator('.run-name').first().textContent(),pinnedName);
  await page.getByRole('button',{name:'Next runs page',exact:true}).click();assert.equal(await page.locator('.run-item').count(),5);
  await page.getByRole('button',{name:'Expand full runs table',exact:true}).click();assert.equal(await page.locator('tbody tr').count(),5);
  await page.getByRole('button',{name:'Collapse runs to sidebar',exact:true}).click();
  await page.getByRole('button',{name:'Previous runs page',exact:true}).click();
  await page.getByRole('button',{name:'Use regular expression for run search'}).click();
  await page.locator('[data-state="search"]').fill('^experiment-0[12]');assert.equal(await page.locator('.run-item').count(),2);
  await page.locator('[data-state="search"]').fill('[');assert(await page.getByRole('alert').filter({hasText:'Invalid regular expression'}).isVisible());assert.equal(await page.locator('.run-item').count(),0);
  await page.locator('[data-state="search"]').fill('');await page.getByRole('button',{name:'Use regular expression for run search'}).click();

  const section=()=>page.locator('[data-section-id="train"]');
  assert.equal(await section().locator('.chart-card').count(),6);
  await page.getByRole('button',{name:'Next panels in train'}).click();assert.equal(await section().locator('.chart-card').count(),6);
  assert(await section().locator('[data-metric="train/metric_06"]').isVisible());
  await page.getByRole('button',{name:'Next panels in train'}).click();assert.equal(await section().locator('.chart-card').count(),2);
  await page.getByRole('button',{name:'Previous panels in train'}).click();await page.getByRole('button',{name:'Previous panels in train'}).click();
  await page.getByRole('button',{name:'Pin train section',exact:true}).click();assert.equal(await page.getByRole('button',{name:'Unpin train section'}).getAttribute('aria-pressed'),'true');
  await page.locator('[data-section-menu="train"]').click();await page.screenshot({path:join(output,'section-actions.png')});
  await page.getByRole('menuitem',{name:'New section above',exact:true}).click();await page.getByLabel('Section name',{exact:true}).fill('Review');await page.getByRole('button',{name:'Apply section'}).click();
  const review=page.locator('.metric-section').filter({has:page.locator('.section-toggle').filter({hasText:'Review'})});
  const reviewId=await review.getAttribute('data-section-id');
  await page.getByRole('button',{name:'Unpin train section'}).click();assert.equal(await page.locator('.metric-section').first().getAttribute('data-section-id'),reviewId);
  await review.getByRole('button',{name:'Add panels',exact:true}).click();await page.locator('input[name="metric"][value="train/metric_00"]').check();await page.getByRole('button',{name:'Add selected panels'}).click();
  const metric='train/metric_00';
  const menu=async(label)=>{await page.locator(`#app [data-panel-menu="${metric}"]`).click();await page.getByRole('menuitem',{name:label,exact:true}).click();};
  await menu('Duplicate panel');
  const copy=page.locator('[data-metric^="panel:"]');assert.equal(await copy.count(),1);assert((await copy.innerText()).includes('(copy)'));
  await copy.locator('[data-edit-panel]').click();await page.locator('#panel-form [name="title"]').fill('Alternate scale');await page.getByRole('button',{name:'Apply settings'}).click();assert.equal(await page.locator(`#app [data-metric="${metric}"] .chart-title`).textContent(),'train/metric_00');
  await page.locator(`[data-expand="${metric}"]`).click();
  await page.locator('#chart-dialog [data-panel-menu]').click();const download=page.waitForEvent('download');await page.getByRole('menuitem',{name:'Download chart as SVG'}).click();assert((await download).suggestedFilename().endsWith('.svg'));
  await page.getByRole('button',{name:'Close expanded chart'}).click();
  await menu('Remove panel from view');assert.equal(await page.locator(`#app [data-metric="${metric}"]`).count(),0);
  await page.locator('[data-action="manage-panels"]').click();await page.locator(`[data-panel-visible="${metric}"]`).check();await page.getByRole('button',{name:'Done',exact:true}).click();assert(await page.locator(`#app [data-metric="${metric}"]`).isVisible());
  await page.locator(`[data-section-menu="${reviewId}"]`).click();await page.getByRole('menuitem',{name:'Hide section'}).click();assert.equal(await review.count(),0);
  await page.locator('[data-action="manage-panels"]').click();await page.getByRole('button',{name:'Show all panels',exact:true}).click();await page.getByRole('button',{name:'Done',exact:true}).click();assert(await review.isVisible());
  // Keyboard menus close without activating an action; sorting is per section.
  await page.locator(`[data-section-menu="${reviewId}"]`).click();await page.keyboard.press('End');assert.equal(await page.locator('[role="menuitem"]:focus').textContent(),'Hide section');await page.keyboard.press('Escape');assert.equal(await page.getByRole('menu').count(),0);
  await page.locator(`[data-section-menu="${reviewId}"]`).click();await page.getByRole('menuitem',{name:'Sort panels A–Z'}).click();assert((await review.locator('.chart-title').first().textContent()).includes('Alternate scale'));
  await page.locator(`[data-section-menu="${reviewId}"]`).click();await page.getByRole('menuitem',{name:'New section below'}).click();await page.getByLabel('Section name',{exact:true}).fill('Following section');await page.getByRole('button',{name:'Apply section'}).click();
  const order=await page.locator('.section-toggle').allTextContents();assert(order[0].includes('Review') && order[1].includes('Following section'));
  await page.getByRole('button',{name:'Use regular expression for panel search'}).click();await page.locator('[data-state="metricSearch"]').fill('metric_1[12]$');assert.equal(await page.locator('#app .chart-card').count(),2);
  await page.locator('[data-state="metricSearch"]').fill('[');assert(await page.getByRole('alert').filter({hasText:'Invalid regular expression'}).isVisible());
  await page.locator('[data-state="metricSearch"]').fill('');await page.getByRole('button',{name:'Use regular expression for panel search'}).click();
  const csvDownload=page.waitForEvent('download');await menu('Download data as CSV');assert((await csvDownload).suggestedFilename().endsWith('.csv'));
  await page.context().grantPermissions(['clipboard-read','clipboard-write']);
  await menu('Copy panel URL');const panelUrl=await page.evaluate(()=>navigator.clipboard.readText());assert.equal(new URL(panelUrl).searchParams.get('panel'),metric);
  await page.goto(panelUrl);await page.getByRole('button',{name:'Close expanded chart'}).waitFor();assert.equal(await page.locator('#chart-dialog h2').textContent(),metric);await page.getByRole('button',{name:'Close expanded chart'}).click();
  const selectedBefore=await saved();await page.locator('.run-name').first().click();assert.equal(await page.locator('.nav-item.active').getAttribute('data-view'),'workspace');await page.getByRole('button',{name:'Exit single-run view'}).click();assert.deepEqual((await saved()).selected,selectedBefore.selected);
  await page.locator('[data-action="save-view-as"]').click();await page.getByLabel('View name',{exact:true}).fill('Review layout');await page.getByRole('button',{name:'Save view',exact:true}).click();
  await page.reload();await page.locator('[data-metric^="panel:"]').waitFor();assert(await page.getByText('On this device',{exact:true}).isVisible());
  assert.equal(await page.locator('.nav-item.active').textContent(),'Workspace');
  await page.screenshot({path:join(output,'navigation-workspace-desktop.png')});
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'No mobile document overflow');
  assert(await page.getByText('On this device',{exact:true}).isVisible(),'Local persistence label remains visible on mobile');
  await page.locator('.nav-item[data-view="workspace"]').focus();await tip.waitFor({state:'visible'});const mobileTip=await tip.boundingBox();assert(mobileTip.x>=8 && mobileTip.x+mobileTip.width<=382);
  await page.screenshot({path:join(output,'navigation-workspace-mobile.png')});await page.keyboard.press('Escape');
  await page.setViewportSize({width:1600,height:1050});
  await page.locator('.header-project').selectOption('demo-user/backprop-demo');
  console.log('PASS: navigation audit, shared run state, pinning, columns, pagination, menus, duplicate/remove/restore, local view reload and responsive layout');
}
