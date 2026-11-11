const task = await taskSpace('symphony-state-consistency-20261111');
console.log({spaceId:task.spaceId});
const page = task.page('p1');
await page.goto('file:///D:/workspace/git/symphony/work/board-state-fixture.html');
await page.waitForSelector('.bcard');
const result = await page.evaluate(async () => {
  const results = [];
  for (const [id, op] of [['GH-TEST1','approve'], ['GH-TEST2','pause'], ['GH-TEST3','resume']]) {
    await postGithubOperation('/api/mock', {}, id, op);
    const card = document.querySelector(".bcard[data-issue='" + id + "']");
    results.push({id, lane:card.parentElement.id, text:card.querySelector('.bstat').textContent});
  }
  // An older read must not restore the original badges or lanes.
  await refreshIssues(true);
  return {results, afterOldRead: [...document.querySelectorAll('.bcard')].map(card => ({
    id:card.dataset.issue, lane:card.parentElement.id, text:card.querySelector('.bstat').textContent
  }))};
});
console.log(JSON.stringify(result));
const expected = {'GH-TEST1':['col-awaiting','已批准 · 等待核验'], 'GH-TEST2':['col-blocked','已转人工 · 正在停止执行'], 'GH-TEST3':['col-ready','已恢复 · 等待调度器核对']};
for (const row of [...result.results, ...result.afterOldRead]) {
  const e=expected[row.id];
  if (!e || row.lane !== e[0] || row.text !== e[1]) throw new Error('unexpected card '+JSON.stringify(row));
}
await task.finish({keep:[]});
