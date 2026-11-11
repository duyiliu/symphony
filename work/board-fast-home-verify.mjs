const task = await taskSpace('symphony-fast-home-verify');
const page = task.page('p1');
await page.goto('http://127.0.0.1:8322/', {timeout: 20000});
console.log(await page.snapshot());
await page.waitForSelector('.bcard', {timeout: 60000});
console.log(await page.snapshot());
await task.finish({keep:['p1']});
