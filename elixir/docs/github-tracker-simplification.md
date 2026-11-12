# Symphony GitHub Tracker 与 Workflow 精简方案

> 状态：设计建议（尚未实施）  
> 范围：个人使用、单仓库先行，后续可扩展多仓库  
> 原则：对齐上游 Symphony 的 GitHub Tracker/Orchestrator，保留少量可靠的无人值守能力；不重新实现 Linear 式 Issue 状态机。

## 一、目标与非目标

目标：GitHub Issue 成为任务事实源；Symphony 自动发现任务、执行开发、验证、提交 PR、响应反馈，在满足 GitHub 保护规则及宿主确定性校验后自动合并并关闭 Issue。遇到无法判定、持续失败或需要真实人工授权的风险时暂停，允许重启后恢复和对账。

非目标：
- 不实现独立的 Issue 编辑器、Sprint、拖拽 Kanban 或第二套项目管理数据库。
- 不以每一步一个 Label 的方式复制 Linear 多状态流。
- 第一阶段不引入消息队列、新数据库、Webhook 服务或多项目调度器。
- 不假定仅改 `WORKFLOW.md` 就能处理 Agent 停止后的 CI/Review 事件。

## 二、现状核对（本仓库）

以下是**现有实现**，不是待开发清单：

| 文件 | 当前作用 |
| --- | --- |
| `elixir/WORKFLOW.github.md` | GitHub 专用示例 Workflow；`agent` / `needs-human`、Workpad、deliver/merge/pause 指令 |
| `elixir/docs/github-workflow.md` | 宿主执行协议、合并校验、风险规则、重试/恢复及部署说明 |
| `board/README.md` | 中文 Issue 看板、运行历史、Pi 日志及人工暂停/恢复操作 |
| `elixir/WORKFLOW.md` | 上游风格 Linear 完整示例，不应直接当作 GitHub 流程 |

现有 Fork 的 `tracker.github_workflow.enabled` 默认关闭，启用后有 **host-enforced** 交付与合并路径。已实现的 JSON receipt、`pending_merge`、`max_attempts` 和风险校验不应因为“简化”而直接删除。实际行为以源码与测试结果为准，而非仅以文档断言。

## 三、职责边界

| 层 | 只负责 | 不负责 |
| --- | --- | --- |
| GitHub Issues / PR / CI | 任务、标签、评论、PR、Review、checks、最终状态的持久事实 | Agent 运行队列 |
| GitHub Tracker | 读取候选 Issue，提供标准化字段和 GitHub API 访问 | 规划、测试、合并策略 |
| Symphony Orchestrator | 并发、工作区、Agent 会话、重试/调度与运行可观测性 | 自建 Issue 业务管理系统 |
| GitHub Workflow（Agent 指令） | 规划、编码、测试、维护 Workpad、创建/复用 PR、处理反馈、请求宿主操作 | 自己批准审核、绕开宿主直接合并 |
| GitHub host workflow（本 Fork 扩展） | 对账、交付 SHA 冻结、合并前确定性校验、预算/暂停、幂等合并与关闭 | 让模型自行裁定安全性 |
| Dashboard | 执行状态、失败、日志、人工待办及 GitHub 跳转 | 维护第二套 Issue 生命周期 |

**关键区分**：`WORKFLOW.md` 是 YAML 配置加 Agent 指令；Tracker 负责取任务；宿主合并校验是代码能力。只编辑 Prompt 无法在 Agent 退出后自行感知新事件或保证合并幂等。

## 四、最小持久状态模型

- **Issue**：GitHub `open / closed`，只表达需求是否完成。
- **调度开关**：`agent` 代表允许自动执行；`needs-human` 代表暂停人工处理；两者同时存在必须 fail closed。
- **风险提示**：`high-risk` 是人工风险声明，不是普通执行阶段；不能替代 GitHub Review Approval。
- **执行瞬态**：`running`、短暂等待和工具进度由 Symphony 维护，不逐一写 Label。
- **恢复事实**：Issue Workpad、PR、checks/reviews、宿主 receipt 共同用于重启对账；不以仅存内存的 blocked 状态作为唯一事实源。

现有代码使用 `agent`，**不要在精简时无意改成 `agent-ready`**，否则与宿主标签协议不兼容。

## 五、推荐的单 Issue 闭环

1. 人工创建 GitHub Issue 并加 `agent`；Tracker 只获取符合仓库、状态及标签条件的候选。
2. Symphony 领取并启动 Agent；读取已有 Workpad、分支与关联 PR，优先恢复而不是重做。
3. Agent 写计划、验收标准，实施、验证、推送，创建或复用同一 PR；PR 用可核验的 closing association 关联 Issue。
4. Agent 通过 `github_workflow(action="deliver")` 提交证据；宿主确认 PR、目标分支及实际 head SHA。
5. Agent 请求 `github_workflow(action="merge")`；宿主读取最新 checks、mergeability、review、风险规则与仓库保护。
6. **可合并**：宿主幂等 squash merge，确认 GitHub 实际合并后关闭 Issue、清理执行标签。
7. **CI 未完成 / Review 等待**：保持等待，不忙轮询；中断后由预算内再次调度/对账处理。
8. **CI 失败 / 冲突 / Review 要求修改**：恢复同一个 PR，修复、重新测试并重新 deliver 新 SHA。
9. **高风险、超预算、不能核验或需要人操作**：写 Workpad 与暂停原因，宿主设置 `needs-human` 并摘除 `agent`；人工处理后移除暂停标签、重新加 `agent`，开启新周期，但此动作不构成合并批准。

## 六、必须保留的安全不变量

- 只有宿主执行最终合并与关 Issue；Agent 不直接调用原始 merge/关闭接口，也不得操作执行标签、保护规则或自行审批。
- 合并时重新核对 PR **当前 head SHA**，不得把旧提交上的检查/批准套用到新提交。
- GitHub 分支保护、必需 CI、Review 和仓库权限始终有效；没有检查报告不等于有质量保证，建议为生产仓库配置必要的 required checks。
- 风险路径、`high-risk` 标签和不可判定条件触发人工门槛；共享 GitHub 凭据并非真正隔离，可信部署范围与权限边界应明确。
- 合并请求前持久化 `pending_merge`，不确定结果先查询 GitHub；避免重复合并与误关 Issue。
- 重试有预算、有退避；反复失败必须暂停，不允许无限热循环。

## 七、建议精简什么

| 项目 | 决策 | 理由 |
| --- | --- | --- |
| 官方 GitHub Tracker | 尽量直接跟随上游 | 减少维护适配器代码 |
| `elixir/WORKFLOW.github.md` | 保留并持续精炼 | GitHub 特有的 PR/反馈闭环不可直接照搬 Linear |
| 宿主 `deliver/merge/pause` | **保留** | 当前无人值守安全合并的关键差异 |
| `agent` / `needs-human` | 保留 | 最小调度意图与跨重启暂停标识 |
| `high-risk` + Review 校验 | 保留 | 高风险人工介入，区别于人工恢复执行 |
| Receipt 与合并幂等 | 保留 | 进程重启和不确定 GitHub API 结果必需 |
| 中文 board 的 Issue 操作 | 可逐步收敛 | 先保留暂停/恢复；其他任务管理尽量跳转 GitHub |
| Dashboard 运行日志与异常 | 保留或与官方面板逐项比对 | 它的价值是观测 Agent，不是第二个 Linear |
| 多项目聚合 | 后置 | 先保证单仓库闭环稳定 |

**不要盲删**之前的 GitHub Workflow/host workflow：它们已覆盖“中间状态 → 重启恢复 → 安全合并”，这恰是纯 Prompt + Open/Closed 无法保证的部分。精简应以删除重复职责为目标，而非删除必要的确定性机制。

## 八、实施顺序（文档先行，暂不修改代码）

1. **基线核对**：对照上游当前 GitHub Tracker、Orchestrator、官方 Dashboard；列出 Fork 改动，分类为必要 / 重复 / 可推迟。
2. **单仓库回归测试**：覆盖普通 Issue→PR→CI→合并→Closed；已有 PR 恢复；CI 失败返工；风险 PR 等待真实 Approval；人工暂停/恢复；中断后重复 merge 请求。
3. **最小代码调整**：仅移除有证据证明重复的实现，保持标签协议和已持久化 receipt 兼容。
4. **看板收敛**：优先查看执行日志、失败、阻塞和需要人工介入的任务；Issue 编辑和 PR Review 跳 GitHub。
5. **稳定后扩展**：需要跨仓库聚合时，再明确 Tracker 单仓库边界与调度器实例/隔离策略。

### 验收标准

- [ ] 普通低风险 Issue 可以无人值守完成合并并关闭。
- [ ] CI 失败、冲突与 Review 反馈不会产生重复 PR，也不会静默跳过。
- [ ] 高风险变更未获当前 head 有效批准时不会合并。
- [ ] 进程重启后能基于 GitHub 事实和 receipt 恢复，不重复 merge。
- [ ] `needs-human` 暂停与人工恢复跨重启生效。
- [ ] Dashboard 不维护独立 Issue 状态机。
- [ ] 与上游差异有清晰说明，升级可逐项复核。

## 九、参考

- [本仓库 GitHub Workflow 详细协议](github-workflow.md)
- [本仓库 GitHub WORKFLOW 示例](../WORKFLOW.github.md)
- [本仓库中文看板说明](../../board/README.md)
- [上游 Symphony](https://github.com/openai/symphony)

> 本文是**收敛方案和验收清单**，不是“所有改造已经完成”的声明。任何运行时精简应先做差异和回归验证。
