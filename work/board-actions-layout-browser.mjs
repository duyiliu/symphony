const task=await taskSpace('symphony-action-layout-20261111');
console.log({spaceId:task.spaceId});
const page=task.page('p1');
await page.goto('file:///D:/workspace/git/symphony/work/board-actions-layout.html');
await page.waitForSelector('.bcard');
await page.evaluate(()=>{
  CUR='GH-TEST2';CARD={state:'running',agent:true,paused:false,mergePending:false};WRITABLE=true;
  dheadEl.textContent='任务操作 · 排版验证';dbodyEl.innerHTML=actionsHtml();
  drawerEl.classList.add('open');shadeEl.classList.add('open');
});
console.log(await page.snapshot());
await page.click('.action-more > summary');
console.log(await page.evaluate(()=>{
  const primary=document.querySelector('#actions .action-toolbar > .abtns button').getBoundingClientRect();
  const more=document.querySelector('.action-more > summary').getBoundingClientRect();
  const menu=document.querySelector('.action-menu').getBoundingClientRect();
  return {primaryTop:primary.top,moreTop:more.top,menuWidth:menu.width,menuInViewport:menu.right<=innerWidth,
    primary:document.querySelector('#actions .abtns button').textContent,menu:document.querySelector('.action-menu').innerText};
}));
console.log(await page.screenshot({path:'D:/workspace/git/symphony/work/board-action-layout.png'}));
await task.finish({keep:[]});
