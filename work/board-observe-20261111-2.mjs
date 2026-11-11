const task = await taskSpace(7);
const page = task.page("p1");
await page.click("text=feat: approve-button e2e (risky path)");
console.log(await page.snapshot({scope:"full_page"}));
