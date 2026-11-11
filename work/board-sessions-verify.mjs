const task = await taskSpace('symphony-session-display-verify');
const page = task.page('p1');
await page.goto('http://127.0.0.1:8322/', {timeout: 60000});
await page.waitForSelector('text=feat: approve-button e2e (risky path)', {timeout: 60000});
await page.click('text=feat: approve-button e2e (risky path)');
await page.waitForSelector('text=已记录会话', {timeout: 60000});
console.log(await page.snapshot({scope:'full_page'}));
await task.finish({keep:['p1']});
