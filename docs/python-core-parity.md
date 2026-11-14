# Python 核心与原版 Symphony 能力矩阵

基线：`upstream/main` = `be10a1b79df723d6d7612b5651c8522704dafb2e`

本文件是“方案二：全 Python 核心”的长期对照表。目标不是逐行翻译 Elixir，
而是让 Python 成为唯一产品运行核心，并以原版行为和测试作为验收基线。
Pi 是唯一受支持的 Agent Runtime；原版 Codex App Server 不移植。

状态说明：

- **已对齐**：Python 已有对应实现，且有行为测试覆盖。
- **部分对齐**：主路径可用，但与原版存在明确差异或覆盖不足。
- **缺失**：当前 Python 没有对应能力。
- **不适用**：属于 Elixir/OTP 或已由 Pi、宿主侧方案替代。

## 架构决策

1. Python 是唯一产品运行入口；`elixir/` 保留为上游参考实现和测试基线，
   在 Phase 4 之前不删除、不作为默认运行路径。
2. Pi 是唯一 Agent Runtime。`runtime.kind: codex` 只保留为明确的
   “不支持”错误，不再作为可运行配置。
3. 行为等价优先于代码结构等价。原版 ExUnit 测试逐项映射为 Python 测试；
   无法映射的 OTP 监督、Phoenix LiveView 等必须记录替代方案。
4. 现有增强能力（安全交付、Agent Session、工作区租约、看板）保留，
   但不得改变原版调度、重试、状态对账和 Tracker 读取语义。
5. Linear + Pi 是默认主链路；Jira、Asana、GitLab 已完成 Tracker 与动态工具，
   SSH Worker 仍属于 Phase 3。

## 模块矩阵

| 原版模块 | 职责 | Python 对应 | 状态 | 说明 |
|---|---|---|---|---|
| `config.ex` / `config/schema.ex` | 配置解析、默认值、校验、环境变量 | `config.py` | 已对齐 | 主体字段已对齐；新增 `runtime`、`delivery`、`server.api_token`；默认 Runtime 为 Pi |
| `workflow.ex` | WORKFLOW.md front matter + prompt | `workflow.py` | 已对齐 | 支持纯 prompt、front matter、非法 front matter 测试 |
| `workflow_store.ex` | 1 秒轮询、last-known-good、热加载 | `workflow_store.py` | 已对齐 | 文件戳 + 内容哈希；失败保留上一版 |
| `prompt_builder.ex` | 严格变量渲染、日期/嵌套对象 | `template.py` | 已对齐 | Liquid tags + 常用 filters、`assign`/`capture`/`case`/`raw`；strict variables/filters 行为一致；错误文本为 Python 风格 |
| `tracker.ex` / `tracker/issue.ex` | Tracker 适配器边界、Issue 规范化 | `tracker.py` / `issue.py` | 已对齐 | Linear、GitHub、Jira、Asana、GitLab 与 board 扩展均已注册；Issue 规范化有行为测试 |
| `linear/*` | Linear 读取、GraphQL 工具 | `linear.py` / `linear_oauth.py` | 已对齐 | 含分页、blockers、scope、OAuth、Agent Session；默认状态、`LINEAR_ASSIGNEE` 和 provider 默认值已对齐 |
| `github/*` | GitHub Issues 读取、API 工具 | `github/*` | 已对齐 | 读取、分页、ID 刷新、404 跳过与 `github_api` 工具已对齐；适配器委托 client，错误按原版语义传播，不静默为空轮询 |
| `jira/*` | Jira Cloud Tracker + 工具 | `jira/*` | 已对齐 | 分页 search/jql、bulkfetch、ADF、blockers、`jira_rest`；32 项测试 |
| `asana/*` | Asana Tracker + 工具 | `asana/*` | 已对齐 | 项目任务分页、按 ID 刷新、404/项目外跳过、`asana_api`；25 项测试 |
| `gitlab/*` | GitLab Tracker + 工具 | `gitlab/*` | 已对齐 | 项目 Issue 分页、IID 刷新、404 跳过、状态映射、`gitlab_api`；16 项测试 |
| `tracker/memory.ex` | 测试用内存 Tracker | 无 | 不适用 | Python 测试直接构造 Issue |
| `orchestrator.ex` | 轮询、领取、并发、重试、stall、对账、Token/Rate Limit | `orchestrator.py` | 已对齐 | 原版 `core_test` / `orchestrator_status_test` 映射已覆盖；snapshot、Token、stall、对账、重试和刷新均有测试 |
| `agent_runner.ex` | 多 turn、活跃状态检查、工作区生命周期 | `agent/runner.py` | 已对齐 | continuation、max_turns、失败分类；同一 worker lifetime 不跨机器 |
| `codex/app_server.ex` | Codex App Server JSON-RPC | `agent/pi_runtime.py` | 不适用 | Pi JSON-RPC 替代；`turn_timeout_ms` 为静默超时，扩展 UI 对话框映射为 `turn_input_required` → blocked；Pi 无 Codex 审批与 rate-limit 事件 |
| `codex/dynamic_tool.ex` | Tracker 动态工具注入 | `mcp_bridge.py` / `tracker.py` | 已对齐 | Linear、GitHub、Jira、Asana、GitLab 均已实现 |
| `workspace.ex` / `path_safety.ex` | 工作区、Hook、路径安全 | `workspace.py` | 已对齐 | 本地与远程路径、Hooks、清理；远程路径只做字符校验，不要求在本机根目录 |
| `ssh.ex` | SSH Worker 执行 | `ssh.py` | 已对齐 | `host:port`、IPv6、`SYMPHONY_SSH_CONFIG`、MCP 反向隧道、超时与 `ssh_not_found` |
| `agent_runtime_supervisor.ex` | OTP 监督树 | Python 线程/进程管理 | 不适用 | 需用生命周期测试保证等价 |
| `http_server.ex` / `endpoint.ex` | HTTP 服务 | `http_api.py` | 已对齐 | `state`、`issue`、`refresh` 已实现，另有 tasks API |
| `observability_api_controller.ex` / `presenter.ex` | JSON 观测契约 | `http_api.py` | 已对齐 | 原版 `state`/`issue`/`refresh` 字段已覆盖，并增加 `metrics`、`health.alerts`、control API |
| `dashboard_live.ex` / `status_dashboard.ex` | Phoenix/终端看板 | `board/` + `status_dashboard.py` | 已对齐 | `board/` 是官方可视化观测面；终端 dashboard 已移植，5 个原版快照逐字节一致 |
| `cli.ex` | CLI 入口、`--workflow`、`--port`、guardrail | `cli.py` | 已对齐 | 支持位置参数、`--workflow`、`--port`、`--host`、`--check`、`--logs-root`、`--log-file`；guardrail 提示和确认参数已移植，仅将 Codex 文案替换为 Pi |
| `log_file.ex` | 轮转日志 | `logging_setup.py` | 已对齐 | 默认 `<cwd>/log/symphony.log`；10 MB × 5 轮转，UTF-8，凭据脱敏 |
| `specs_check.ex` / mix tasks | 仓库维护工具 | `pr_body_check.py`、`workspace_before_remove.py` | 已对齐 | 两个 dev task 已移植并有 `test_dev_tools.py` 覆盖 |
| Burrito release | 单文件发布 | PowerShell/systemd | 不适用 | Python 发布方式另行设计 |

## 现有增强（原版没有）

| 能力 | Python 位置 | 边界 |
|---|---|---|
| 宿主侧安全交付 | `delivery.py`、`github/workflow_policy.py` | 合并只能由宿主执行；Pi 不持有合并凭据 |
| Agent Session 生命周期 | `agent_sessions.py` | OAuth、进度、终态、outbox、重启恢复 |
| MCP scope 隔离 | `mcp_bridge.py` | 每次 Worker 独立 bearer；Issue/项目 scope；AST allowlist；停止请求即撤销 |
| 工作区跨进程租约 | `workspace.py` | 防止同一 Issue 双执行；终止未确认时 fail-closed |
| 中文看板 | `board/` | 七态泳道、日志、历史、人工接管、多项目只读导入 |
| tasks API | `http_api.py`、`tasks/` | 任务导入、评论、PR 关联等扩展接口 |

## 当前差距与优先级

### P0：Linear + Pi 闭环（Phase 1 已完成）

1. **唯一 Runtime 契约（已完成）**：默认 `runtime.kind` 为 `pi`；`codex` 明确拒绝；
   `runtime.command` 在启动前预检，避免运行到 Worker 才报错。
2. **原版测试映射（已完成）**：把 `core_test`、`workspace_and_config_test`、
   `orchestrator_status_test`、`app_server_test`、`dynamic_tool_test`
   逐项映射为 Python 测试，标注“已覆盖 / 替代覆盖 / 不适用”。
3. **Prompt 严格语义（已完成）**：filters、日期、嵌套 map、缺失变量的行为与
   Solid 严格模式一致，错误信息需包含模板上下文。
4. **工作区与 Hook 边界（已完成）**：本地路径安全、符号链接逃逸、Hook 超时/失败、
   远程未支持必须显式失败，不能静默降级。
5. **Pi 人工阻塞契约**：Pi 扩展 UI 的 `select`/`confirm`/`input`/`editor`
   已映射为 `turn_input_required` 并进入 `blocked`；但 Linear 人工答案、
   停止信号仍不能回传 Pi，恢复只能通过宿主重试或重新领取，停止通过终止
   Pi 进程树实现。

### P1：长期无人值守运行

6. **统一生命周期与启动对账**：Tracker 状态、调度内存、delivery JSON、
   Agent Session JSON 的事实源和启动扫描顺序。
7. **可观测性（已完成）**：领取延迟、运行时长、重试率、阻塞率、outbox 积压、
   MCP scope 使用量、终止未确认数量，以及 `health.alerts` 阈值告警。
8. **灾备与运维（已完成）**：日志保留、暂停/恢复调度、释放异常 Worker、
   清理工作区、周期状态备份和 `--restore-state` 恢复均已实现，
   见 `docs/operations.md`。
9. **启动预检（已完成）**：`--check` 检查工作区可写性和可选凭据；致命错误与
   能力降级分开报告。
10. **观测面（已完成）**：明确 `board/` 为官方可视化观测面，
    `/api/v1/state` 为机器可读事实源。

### P2：外围能力（当前不阻塞 Linear + Pi）

11. Jira、Asana、GitLab 已完成 Tracker 与动态工具。
12. SSH 远程 Worker 已完成：workspace、hooks、cleanup、agent runner、host 并发；
    远程 MCP 使用 SSH 反向隧道，隧道失败即拒绝启动。
13. Codex App Server 不移植；文档明确 Pi 的能力边界。
14. Elixir 执行链退役（已完成）：入口、CI、发布物、文档已切到 Python；
    Burrito 发布改为手动；`elixir/` 源码归档或删除仍需单独决策。

### 远程 Worker 边界

- 远程 workspace 路径不做本地 canonicalize，只校验空值、换行、回车和 NUL；
  `~` 由远程 shell 展开，创建后以远程 `pwd -P` 作为真实路径。
- MCP bridge 仍是宿主 `127.0.0.1` 服务；远程 Pi 通过
  `ssh -R 127.0.0.1:<port>:127.0.0.1:<port>` 访问，并设置
  `ExitOnForwardFailure=yes`。远程端口被占用时 SSH 退出，Pi 启动失败，不静默降级。
- 远程 lease 保存在宿主本地状态目录（`SYMPHONY_LEASE_ROOT`，默认系统临时目录），
  不会在远程 workspace root 下创建锁目录。
- 远程 Hooks 在远程用户登录环境中执行，宿主环境变量不会自动转发；
  deploy key、仓库地址和 Git 身份必须在远程主机配置。宿主 Tracker/GitHub
  凭据仍会从 Pi 环境中剥离。
- `stop_session` 确认的是本地 SSH 客户端进程树死亡；远程 Pi 依赖 SSH 断连后的
  SIGHUP，未做远程进程扫描。原版同样只关闭 SSH 端口，这是明确的等价边界。
- 远程 `.pi/mcp.json` 含 worker bearer，权限为 `0600`；远程主机必须按可信执行环境管理。

## 验收方法

- 每个原版测试必须在 `work/任务/全Python核心移植/tasks.md` 的映射表中
  有对应 Python 测试或“不适用”理由。
- `python -m unittest discover -s python/tests -q` 与
  `python -m pytest -q python/tests` 必须全绿。
- 配置、工作区、编排器、HTTP API 的关键行为不能只靠 mock；
  至少保留一条真实 Linear + Pi 闭环证据。
- 真实闭环与故障注入分开记录；不得用单元测试通过代替现场验收。

2026-11-14 已在 Symphony E2E 项目完成 Phase 1.8 真实闭环（YIL-182，
PR `duyiliu/symphony-e2e#21`，merge commit `e18b992`），证据和当前工作树
SHA-256 见 `work/任务/全Python核心移植/tasks.md` 的 1.8 节。
