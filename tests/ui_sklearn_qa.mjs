/** Actual SDK controls -> production renderers -> visible labels and panels.
 * Main callers: browser_qa.mjs. Optional sklearn absence is reported explicitly;
 * a present numerical runtime must generate valid real helper outputs.
 */
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {join} from 'node:path';

export function sklearnFixture(root,python){
 const result=spawnSync(python,['-B',join(root,'tests/sklearn_replay_fixture.py')],{encoding:'utf8',maxBuffer:8*1024*1024});
 assert.equal(result.status,0,`sklearn fixture failed: ${result.stderr}`);
 return JSON.parse(result.stdout);
}

export async function verifySklearnUI(page,fixture,output){
 if(fixture.unavailable)return;
 await page.locator('.header-project').selectOption('e/sklearn-replay');
 await page.locator('[data-view="data"]').first().click();
 await page.locator('[data-state="richType"]').selectOption('');
 await page.locator('[data-state="richSearch"]').fill('');
 await page.locator('[data-state="richRun"]').selectOption('');
 const card=key=>page.locator('.rich-card').filter({has:page.getByRole('heading',{name:key,exact:true})});
 const feature=card('feature_importances'),elbow=card('elbow_curve'),silhouette=card('silhouette_plot'),roc=card('roc');
 await feature.locator('[data-vega-ready="true"]').waitFor();
 assert((await feature.locator('svg.marks').textContent()).includes('Ranked features'));
 const featureRows=fixture.expected.history.filter(row=>row.feature_importances).at(-1).feature_importances.table.data;
 assert.equal(featureRows.length,2);
 const featureText=await feature.locator('svg.marks text').allTextContents();
 for(const row of featureRows)assert(featureText.includes(String(row[0])));
 await elbow.locator('[data-vega-ready="true"]').waitFor();
 assert(!(await elbow.locator('svg.marks').textContent()).includes('Clustering time'));
 const timed=fixture.expected.history.find(row=>row.elbow_curve?.spec?.vconcat)?._step;
 assert.notEqual(timed,undefined);
 await elbow.getByRole('combobox',{name:'History step for elbow_curve',exact:true}).selectOption(String(timed));
 await elbow.locator('[data-vega-ready="true"]').waitFor();
 assert((await elbow.locator('svg.marks').textContent()).includes('Clustering time'));
 await elbow.getByRole('combobox',{name:'History step for elbow_curve',exact:true}).selectOption('');
 await silhouette.locator('[data-vega-ready="true"]').waitFor();
 const clusterText=await silhouette.locator('svg.marks text').allTextContents();
 for(const name of ['A','B','C'])assert(clusterText.includes(name));
 const aggregate=fixture.expected.history.find(row=>row.roc?.table?.data.some(values=>values[0]==='micro-average'))?._step;
 assert.notEqual(aggregate,undefined);
 await roc.getByRole('combobox',{name:'History step for roc',exact:true}).selectOption(String(aggregate));
 await roc.getByText('micro-average',{exact:true}).waitFor();
 await roc.getByText('macro-average',{exact:true}).waitFor();
 await page.locator('#toast').waitFor({state:'hidden'});
 await feature.screenshot({path:join(output,'sklearn-features.png')});
 assert((await silhouette.locator('svg.marks').boundingBox()).height<650);
 await silhouette.screenshot({path:join(output,'sklearn-clusters.png')});
 await page.setViewportSize({width:390,height:844});
 await feature.scrollIntoViewIfNeeded();
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 await feature.screenshot({path:join(output,'sklearn-features-mobile.png')});
 await page.setViewportSize({width:1600,height:1050});
 await page.locator('.header-project').selectOption('demo-user/backprop-demo');
}
