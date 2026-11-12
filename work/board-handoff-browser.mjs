const task = await taskSpace('symphony-handoff-v2-20261111');
console.log({spaceId:task.spaceId});
const page = task.page('p1');
await page.goto('file:///D:/workspace/git/symphony/work/board-handoff-fixture.html');
await page.waitForSelector('.bcard[data-issue="GH-1"]');
console.log(await page.snapshot());
const lanes = await page.evaluate(() => [...document.querySelectorAll('.bcard')].map(c=>({id:c.dataset.issue,state:c.dataset.state,text:c.dataset.stateText})));
console.log(lanes);
for(const [id,lane] of [['GH-1','awaiting'],['GH-2','awaiting'],['GH-3','running'],['GH-4','blocked'],['GH-5','idle'],['GH-6','awaiting'],['GH-7','idle'],['GH-8','ready']]) {
 if(lanes.find(c=>c.id===id)?.state!==lane) throw Error('Wrong lane '+id);
}
await page.evaluate(() => {
 window.openFixture = id => {
  const c=document.querySelector(`.bcard[data-issue="${id}"]`);
  CUR=id;CARD={name:id,state:c.dataset.state,text:c.dataset.stateText,agent:c.dataset.agent==='1',paused:c.dataset.paused==='1',mergePending:c.dataset.mg==='1',approved:false,approvable:c.dataset.approvable==='1',stage:c.dataset.stage,sha:c.dataset.sha,deliveryId:c.dataset.deliveryId,prNum:c.dataset.prNum,pr:c.dataset.pr};
  WRITABLE=true;dbodyEl.innerHTML=actionsHtml();dheadEl.textContent=id;drawerEl.classList.add('open');shadeEl.classList.add('open');
 };
 openFixture('GH-1');
});
console.log(await page.snapshot());
await page.click('[data-label="merge-approved"][data-act="add"]');
await page.waitForSelector('#dlg-ok',{state:'visible'});
await page.click('#dlg-ok');
await page.waitForFunction(()=>window.testWrites.length===1);
const approval = await page.evaluate(()=>window.testWrites[0]);
if(approval.body.delivery_id!=='delivery-1'||approval.body.sha!=='a'.repeat(40)) throw Error('Approval version not bound');
await page.evaluate(()=>{PENDING_OPERATIONS.clear();openFixture('GH-2');});
const noApprove = await page.evaluate(()=>!document.querySelector('[data-label="merge-approved"][data-act="add"]'));
if(!noApprove) throw Error('Pending CI unexpectedly approvable');
await page.click('[data-rework]');
await page.waitForSelector('#dlg-input',{state:'visible'});
await page.fill('#dlg-input','请继续优化可读性，并补充验证。');
await page.click('#dlg-ok');
await page.waitForFunction(()=>window.testWrites.length===2);
const rework = await page.evaluate(()=>({write:window.testWrites[1],lane:document.querySelector('[data-issue="GH-2"]').dataset.state}));
if(rework.write.url!=='/api/rework'||rework.lane!=='ready') throw Error('Rework did not queue');
await page.evaluate(()=>{PENDING_OPERATIONS.clear();openFixture('GH-4');});
console.log(await page.snapshot());
await page.waitForSelector('[data-intervene="resume"]');
console.log({approval,rework,noApprove,pausedResume:true});
await task.finish({keep:[]});
