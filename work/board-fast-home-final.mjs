const task = await taskSpace('symphony-fast-home-final');
const page = task.page('p1');
await page.goto('http://127.0.0.1:8322/', {timeout: 20000});
console.log(await page.evaluate(() => ({summary: document.querySelector('#sum').textContent, firstColumn: document.querySelector('#col-running').textContent})));
await page.waitForSelector('.bcard', {timeout: 60000});
console.log(await page.evaluate(() => ({summary: document.querySelector('#sum').textContent, cards: document.querySelectorAll('.bcard').length})));
await task.finish({keep:['p1']});
