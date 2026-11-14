# 全 Python 核心移植设计基线

## 1. 背景

当前仓库同时保留了：

- `elixir/`：OpenAI Symphony 原版参考实现，与 `upstream/main` 逐字节一致。
- `python/`：后来新增的 Python Orchestrator + Pi Runtime，已经成为实际运行主线。

问题不是原版源码丢失，而是“替换核心”的边界没有先定义清楚，导致 Python
只实现了 Linear + Pi 所需的一部分外围能力。方案二的目标是：以原版行为为基线，
把 Python 做成唯一产品核心，而不是继续维护两套并行的主实现。

## 2. 目标

1. Python 成为唯一产品运行入口。
2. Pi 成为唯一 Agent Runtime。
3. 原版 Linear + Codex 主链路中的调度、Tracker 读取、工作区、Hook、重试、
   stall、状态对账、HTTP 观测和 CLI 行为，在 Python 中有等价实现和测试。
4. 原版没有、但当前已实现的增强能力继续保留：
   安全交付、Agent Session、MCP scope、工作区租约、中文看板。
5. Elixir 保留为上游参考实现和测试基线；是否删除在 Phase 4 单独决策。

## 3. 非目标

- 不逐行翻译 Elixir/OTP 代码，不复制 GenServer/Supervisor 结构。
- 不在 Phase 1 移植 Jira、Asana、GitLab Tracker 和 SSH Worker；这些已在
  Phase 3 完成，不再扩大多 Tracker 统一调度。
- 不移植 Codex App Server；Pi 是唯一 Runtime。
- 不把 Phoenix LiveView 原样搬到 Python；`board/` 作为观测与人工操作面。
- 不改变当前 Linear + Pi 的业务流程和安全交付边界。

## 4. 目标架构

```text
WORKFLOW.md
  -> WorkflowStore (last-known-good, hot reload)
  -> Settings (schema validation, env indirection)
  -> Tracker Adapter (Linear first)
  -> Orchestrator (poll, claim, concurrency, retry, stall, reconcile)
  -> Workspace + Hooks (local or SSH worker)
  -> Agent Runner (multi-turn, active-state continuation)
  -> Pi Runtime (JSON-RPC, approval/input blockers, token/rate-limit events)
  -> MCP Bridge (scoped bearer, Linear/GitHub tools)
  -> HTTP API (state/issue/refresh, health, tasks)
  -> Delivery Controller (host-side PR gates and merge)
  -> Agent Session Reporter (OAuth, activity, outbox)
```

职责边界：

- Tracker 是业务状态的唯一事实源；Orchestrator 不自行发明业务状态。
- Orchestrator 内存是调度事实；重启后通过 Tracker 和工作区重新对账。
- Delivery JSON 是交付事实；合并只能由宿主执行。
- Agent Session JSON 是上报事实；outbox 必须可恢复、可去重。
- Pi 只执行代码和调用受限工具，不持有合并凭据。

## 5. 生命周期与对账

目标启动顺序：

1. 加载并校验 `WORKFLOW.md`，失败则拒绝启动。
2. 预检 Runtime、Tracker、凭据和必需目录；区分致命错误与能力降级。
3. 恢复 Agent Session outbox 和 delivery 状态。
4. 读取 Tracker 活跃/终态 Issue。
5. 清理终态 Issue 的历史工作区。
6. 恢复或释放工作区租约，再开始轮询和派发。

调度状态：

- `running`：Worker 已启动，持有 claim 和 slot。
- `retrying`：等待退避重试，保留 Issue 元数据。
- `blocked`：需要人工输入、审批、凭据或配置修复。
- `claimed`：已领取但尚未进入 running/retrying/blocked 的过渡态。

交付状态与调度状态分离：

- `Human Review`、`Merging`、`Rework`、`Done` 由 delivery 控制器消费。
- `Human Review`、`Merging` 不得出现在 `tracker.active_states` 中。
- `Done` 必须是 terminal state。

## 6. 分阶段实施

### Phase 0：基线（本轮）

- 建立 `docs/python-core-parity.md` 能力矩阵。
- 建立本设计基线和任务清单。
- 默认 `runtime.kind` 改为 `pi`，更新配置测试。
- 明确 Elixir 只作为参考实现，不作为默认运行路径。

### Phase 1：Linear + Pi 行为 1:1

- 配置/Workflow/Prompt：默认值、校验、严格变量、日期和嵌套对象。
- Tracker 接口：统一 `fetch_issues_by_states`、`fetch_issues_by_ids`、
  `agent_tool_specs`、`execute_agent_tool`、`validate_config`。
- Orchestrator：补齐原版测试映射，覆盖重试、stall、阻塞、对账、
  Token/Rate Limit、按状态并发上限。
- Workspace/Hooks：路径安全、符号链接、Hook 超时/失败、清理边界。
- Pi Runtime：turn timeout、stall、审批、用户输入、MCP elicitation、
  token/rate-limit 事件。
- HTTP/CLI/日志：`state`、`issue`、`refresh`、`--check`、轮转日志。
- 至少一条真实 Linear + Pi 闭环证据。

### Phase 2：观测与运维

- 终端 dashboard 或明确 `board/` 为官方观测面。
- 指标：领取延迟、运行时长、重试率、阻塞率、outbox 积压、scope 使用量。
- 备份/恢复、日志保留、依赖不可用时的降级策略。
- 启动预检和统一运维入口（暂停、释放 Worker、清理工作区）。

### Phase 3：外围适配

- 已完成 Jira、Asana、GitLab Tracker 与动态工具。
- 已完成 SSH Worker：workspace、hooks、cleanup、agent runner、host 并发。
- 已完成对应原版 adapter 的 Python 等价测试；live e2e 仍需现场验证。

### Phase 4：退役 Elixir 执行链

- 默认入口、CI、发布物、文档全部切换到 Python。
- `elixir/` 归档或删除需单独决策，不与功能移植混在一起。

## 7. 测试等价策略

原版测试是行为基线，不要求测试代码同名。映射分三类：

1. **直接覆盖**：Python 测试断言同一行为。
2. **替代覆盖**：OTP/Phoenix 结构不同，但对外行为由 Python 测试覆盖。
3. **不适用**：Elixir 专属实现细节，记录理由。

优先映射：

- `core_test.exs`：配置、启动、重试、对账、Prompt、Agent Runner。
- `workspace_and_config_test.exs`：工作区、路径安全、Hook、配置默认值。
- `orchestrator_status_test.exs`：snapshot、Token、Rate Limit、stall、阻塞。
- `app_server_test.exs`：turn timeout、审批、动态工具、凭据剥离。
- `dynamic_tool_test.exs`：工具参数校验、错误载荷、绑定快照。

每个映射项必须能指向具体 Python 测试；不能只写“已实现”。

## 8. 风险

- **两套实现漂移**：在 Phase 4 前，Elixir 仍可能被误用。文档和 CLI 必须明确
  Python 是唯一产品入口。
- **Pi 与 Codex 协议差异**：审批、沙箱、MCP elicitation 不能假设完全等价；
  必须按 Pi 实际事件验证。
- **外围能力扩张**：Jira/Asana/GitLab/SSH 已按 Phase 3 收口，不再新增
  多 Tracker 统一调度或其他平台接入。
- **测试通过不等于现场可用**：真实 Linear + Pi 闭环、GitHub 分支保护、
  合并响应丢失等必须单独验收。

## 9. 退出标准

Phase 1 完成时：

- Python 全量 unittest/pytest 全绿。
- Linear + Pi 主链路有真实闭环证据。
- 原版核心测试映射表无“未解释”项。
- 默认配置、启动预检、CLI 和 HTTP API 行为有测试。

Phase 4 完成时：

- 仓库默认运行路径只有 Python。
- Elixir 不再参与发布和 CI 必需路径。
- 原版能力矩阵中所有“缺失/部分对齐”项都有结论：已补齐、明确不适用或已退役。
