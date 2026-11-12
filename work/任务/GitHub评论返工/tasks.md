# 执行与交付分离 v2 - 执行队列

基线 main / e5acc05；保留已有 board、runner、workflow、orchestrator 及测试未提交改动。设计依据用户“按照这个方式实施吧”。
实现审查：PASS；Reviewer rework_review；第 2/3 轮完整增量复审通过，第 3/3 轮收尾增量复核通过，无未解决 Critical/High。不提交推送、不恢复/批准 GH155。

- T01 done：AC01-05，宿主持久接管/独立检查/重启恢复/新授权使旧交付失效/累计修复预算/批准绑定。
- T02 done：AC03、06，WORKFLOW 定义意见处理，API 输出阶段，看板按钮/卡片统一；保留失败/部分成功/不确定结果。
- T03 done：AC01-07，回归、隔离浏览器、独立审查、空闲加载及服务/暂停任务核对均完成。

## 验证
- `python -m pytest python/tests board/tests -q --tb=short`：187 passed，退出码 0。过程中 Windows HTTP 用例曾遇 WinError 10053；该用例单跑及随后整套回归均通过，未隐藏失败或跳过覆盖。
- `node --test board/tests/test_refresh.mjs`：19 passed，退出码 0；`git diff --check`：通过（已有 LF/CRLF 提示）。
- Ego 隔离浏览器验收：8 种卡片状态、批准版本参数、等待 CI 隐藏批准、继续优化排队、暂停保留恢复入口；通过，测试空间已关闭，无真实 GitHub 写入。
- 宿主审批 API：无凭据写入实际返回 403；交付版本/授权和远端部分失败由离线 HTTP/生命周期用例覆盖。
- 本地已加载：调度器 PID 50940，看板 PID 37368，各一个监听进程；8320/8322 均 HTTP 200，首页约 2ms（本次取样），state 包含 github_workflows。
- 加载后 running=0/retrying=0，GH155 为 needs-human/paused 且 can_approve=false。旧 GH139 已合并事实只读确认并补齐本地 merged 收据。未恢复 GH155，未发起新 AI 或真实合并测试。
- 本轮仅本地服务加载及 deploy/WORKFLOW.199.md 模板更新；未部署远程 199，未提交推送。
