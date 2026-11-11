const task = await taskSpace("symphony-board-observe-20261111");
console.log({spaceId: task.spaceId});
const page = task.page("p1");
await page.goto("http://127.0.0.1:8322/", {timeout: 45000});
console.log(await page.snapshot({scope: "full_page"}));
