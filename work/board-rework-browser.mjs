const task = await taskSpace('symphony-pr-rework-20261111');
console.log({spaceId: task.spaceId});
const page = task.page('p1');
await page.goto('file:///D:/workspace/git/symphony/work/board-rework-fixture.html');
await page.waitForSelector('.bcard');
await page.evaluate(() => {
  CUR = 'GH-TEST1';
  CARD = {state:'awaiting',agent:true,paused:false,mergePending:true,approved:false,
    prNum:'2',pr:'https://github.com/fixture/repo/pull/2'};
  WRITABLE = true;
  dheadEl.textContent = '继续优化 · 隔离验证';
  dbodyEl.innerHTML = actionsHtml();
  drawerEl.classList.add('open');shadeEl.classList.add('open');
  window.testWrites = [];
  const fakeFetch = window.fetch;
  window.fetch = async (url, opts) => {
    if (opts?.method === 'POST') window.testWrites.push({url,body:JSON.parse(opts.body)});
    return fakeFetch(url, opts);
  };
});
console.log(await page.snapshot());
await page.click('[data-rework]');
await page.waitForSelector('#dlg-input', {state:'visible'});
await page.fill('#dlg-input', '请将主题配色收敛，并补充可读性验证。');
console.log(await page.snapshot());
await page.click('#dlg-ok');
await page.waitForFunction(() => PENDING_OPERATIONS.has('GH-TEST1'));
const result = await page.evaluate(() => ({
  writes:window.testWrites,
  state:document.querySelector('[data-issue="GH-TEST1"]').getAttribute('data-state'),
  text:document.querySelector('[data-issue="GH-TEST1"]').getAttribute('data-state-text'),
  notice:document.querySelector('#actions')?.innerText
}));
console.log(result);
if (result.writes.length !== 1 || result.writes[0].url !== '/api/rework'
  || result.state !== 'ready' || !result.text.includes('返工')) throw new Error('Rework UI verification failed');
console.log(await page.screenshot({path:'D:/workspace/git/symphony/work/board-rework-result.png'}));
await task.finish({keep:[]});
