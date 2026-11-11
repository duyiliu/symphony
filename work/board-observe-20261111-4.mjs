const task = await taskSpace(7);
const page = task.page("p1");
await page.click("@13");
await page.click("@5");
await page.waitForTimeout(3000);
console.log(await page.snapshot({scope:"full_page"}));
await task.finish({keep:["p1"]});
