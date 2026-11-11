const task = await taskSpace(7);
const page = task.page("p1");
await page.waitForTimeout(5000);
console.log(await page.snapshot({scope:"full_page"}));
