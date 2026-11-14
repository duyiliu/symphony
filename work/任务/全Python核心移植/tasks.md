# 全 Python 核心移植任务

设计基线：`design.md`
能力矩阵：`docs/python-core-parity.md`

状态：`[ ]` 未开始，`[~]` 进行中，`[x]` 已完成。

## Phase 0：基线与契约

- [x] 建立原版模块 ↔ Python 能力矩阵。
- [x] 建立设计基线和阶段边界。
- [x] 默认 `runtime.kind` 改为 `pi`，并更新配置测试。
- [x] CI 增加 Python unittest、pytest、board 测试和 `compileall`。
- [x] 建立原版 ExUnit → Python 测试映射表（见 `test-mapping.md`）。
- [x] 在 `python/README.md` 明确 Python 是唯一产品入口、Elixir 仅参考。

验收：

- 矩阵中每个“缺失/部分对齐”项都有阶段归属。
- 默认配置不再落到不可运行的 `codex` Runtime。

## Phase 1：Linear + Pi 行为 1:1

### 1.1 配置与 Workflow

- [x] 启动前预检 `runtime.command`，缺失时返回 `missing_pi_command`。
- [x] `runtime.kind: codex` 在加载 Workflow 时明确拒绝。
- [x] 核对原版配置默认值、空值、环境变量间接引用和路径解析。
- [x] 核对 `WORKFLOW.md` 纯 prompt、未闭合 front matter、非 map front matter。

有意差异：Python 额外解析 `tracker.project_slug` 的 `$VAR`，并扩展
`secret_environment_names`（OAuth/GitHub 等），用于 Pi 子进程凭据剥离；
`tracker.provider` 仍保留原始 `$VAR`，与原版一致。

### 1.2 Prompt

- [x] 对齐 Solid 严格变量、日期、嵌套 map/struct 的渲染。
- [x] 明确不支持 filters 的行为和错误文本。
- [x] 补 `attempt`、缺失字段、非法模板的测试。
- [x] 实现 Solid 兼容的 Liquid filters、`assign`、`capture`、`case`、`raw`。

模板解析失败包装为 `template_parse_error: ... template=...`；严格变量和
未知 filter 在渲染阶段抛 `TemplateError`，对应原版 `Solid.RenderError`。
`python/tests/test_liquid_filters.py`（23 项）覆盖字符串、集合、数值、
日期/JSON filters，链式调用、条件/循环中的 filter，以及 `assign`、
`capture`、`case/when/else`、`raw` 和严格错误；`render` 不再修改调用方
context，循环内 `assign` 保持 Liquid 的循环作用域语义。

### 1.3 Tracker

- [x] 抽出统一 Tracker 协议，Linear 先实现。
- [x] 对齐 `fetch_issues_by_states`、`fetch_issues_by_ids`、分页和空集合行为。
- [x] 对齐 Issue 字段、blockers、labels、assignee、dispatchable。
- [x] 对齐 `linear_graphql` 参数校验、错误载荷和绑定快照。
  错误载荷保留 Python 的机器可读 `code/detail`，由 MCP Bridge 统一转换为 `isError` 和文本。
- [x] 对齐 Linear 默认状态、`LINEAR_ASSIGNEE` 回退和 provider 默认值。

### 1.4 Orchestrator

- [x] 映射 `core_test.exs` 的启动、重试、对账、并发和 Prompt 测试。
- [x] 映射 `orchestrator_status_test.exs` 的 snapshot、Token、Rate Limit、stall。
- [x] 覆盖非活跃状态停止 Worker、终态清理工作区、Issue 消失、重新分配。
- [x] 覆盖 required labels 移除、blocked 释放、retry claim 释放。
- [x] 覆盖按状态并发上限和 SSH host 选择（远程执行仍可不支持）。

补充覆盖：run lease、控制面 deferral、stall 重试、retry token 过期、
manual refresh 合并、终态清理使用记录的工作区、启动预检和失败保留旧配置。
原版 `core_test` / `orchestrator_status_test` 映射已全部有结论；
`docs/python-core-parity.md` 中 `orchestrator.ex` 已更新为“已对齐”。

### 1.5 Workspace 与 Hooks

- [x] 覆盖路径确定性、符号链接逃逸、工作区根目录保护。
- [x] 覆盖 `after_create`、`before_run`、`after_run`、`before_remove`。
- [x] 覆盖 Hook 失败、超时、大输出、清理继续执行。
- [x] 远程 Worker 已完成；本地/远程不得静默降级，SSH 或隧道失败必须显式报错。

本地覆盖在 `python/tests/test_workspace_hooks.py`（18 项）：确定性命名、
relative root、symlink root 规范化、symlink 逃逸拒绝、root 保护、
recorded 路径逃逸在 hook 前拒绝、复用不删本地内容、stale 非目录替换、
`after_create` 失败清理并重试、超时、日志截断而错误保留完整输出、
`before_run` 传播、`after_run`/`before_remove` 失败不阻断、悬空链接删除。
Windows Git Bash 会在 hook 输出前带 ANSI 清屏序列，测试按“完整输出”
断言，不修改命令输出本身。

### 1.6 Pi Runtime

- [x] 覆盖 turn timeout、stall、进程终止确认和终止未确认。
- [x] 覆盖审批、用户输入、MCP elicitation 进入 blocked。
- [x] 覆盖 token usage、rate limit、工具调用失败和 malformed JSONL。
- [x] 覆盖 Pi 子进程凭据剥离和 MCP bearer scope。
- [x] 明确人工答案/停止信号进入 Pi 的路径；未实现前在文档标注。

`run_turn` 的 `turn_timeout_ms` 改为原版的静默超时语义：任意协议行都会重置
截止时间，只有 stdout 持续安静才失败。Pi 扩展 UI 的 `select`/`confirm`/
`input`/`editor` 映射为 `turn_input_required`，orchestrator 记录为 blocked
而不是重试；`notify`/`setStatus` 等 fire-and-forget 请求不阻塞。
`python/tests/test_pi_runtime.py`（10 项）覆盖静默超时、取消、坏 JSON、
非对象 JSON、partial 末行、对话框阻塞、工具失败、EOF 退出、usage 累计和
凭据 unset。进程终止确认/未确认沿用 `test_workspace_lease.PiStopTest` 与
`test_orchestrator_units.StopLifecycleTest`；stall 沿用 `StallTest`。

有意差异：Pi 没有 Codex 的 `account/rateLimits/updated` 事件，rate limit
快照不适用；Pi 用 `--approve` 与工具白名单替代 Codex 审批策略。人工答案
仍不能注入现有 Pi session，停止通过终止进程树实现，已在
`docs/local-agent-sessions.md` 与 `docs/python-core-parity.md` 标注。

### 1.7 HTTP、CLI、日志

- [x] 对齐 `/api/v1/state`、`/api/v1/:issue`、`/api/v1/refresh` payload。
- [x] 覆盖 404、405、503 和 refresh 合并请求。
- [x] 核对 CLI `--workflow`、`--port`、`--check`、日志参数。
- [x] 核对轮转日志、UTF-8、敏感字段脱敏。

`python/tests/test_http_api.py`（9 项）覆盖 state/issue/refresh 字段、
snapshot 超时内联错误、running/blocked 详情、404、405、503 和 refresh
coalesced。修正 `GET /api/v1/refresh` 返回 405（Phoenix 路由顺序优先于
`:issue_identifier`）。

CLI 增加位置参数 workflow path、`--logs-root`；`--workflow` 与
`--log-file` 保留为增强。日志默认写 `<cwd>/log/symphony.log`，轮转
10 MB × 5，UTF-8；text/JSON 两种格式均脱敏
`token`/`authorization`/`api_key`/`password`/`secret` 与 `Bearer`。
`python/tests/test_logging_cli.py`（13 项）覆盖默认路径、轮转参数、UTF-8、
两种格式脱敏和 CLI 参数解析。

原版的 `--i-understand-that-this-will-be-running-without-the-usual-guardrails`
确认横幅已移植；Python 仅将 Codex 文案替换为 Pi，并保留 `--check` 和
`--restore-state` 免确认。

### 1.8 真实闭环

- [x] 在测试 Linear 项目跑通 Linear → Pi → PR → CI/门禁 → 合并 → Done。
- [x] 记录工作树 SHA、Issue、PR、CI、合并结果和 Agent Session 终态。
- [x] 故障注入与真实闭环分开记录。

2026-11-14 用当前工作树（`main@7ab1430` 加未提交改动，`git diff`
SHA-256 `79cdce3c740fe927db72ecd7c90e00c60f4cf7532dc4b6ccfa4f640589f79b59`）
冷启动本机 E2E 服务，跑通 YIL-182：

- 19:22:56 自动领取，工作区 `D:\symphony-e2e\workspaces\YIL-182`；
  19:23:02 启动 Pi，19:25:05 进入 `Human Review`。
- PR `duyiliu/symphony-e2e#21`，HEAD
  `f650de9151fa962a265a21c65b5899bdbe50c27d`；3 个 `test` 检查全部成功。
- 宿主门禁 11:25:25Z 合并，merge commit
  `e18b992b3ca38a222e7efa2c0c72cd1797bcd64a`；Linear 更新为 `Done`。
- delivery 记录 `phase=merged`、`merge_attempts=1`；Agent Session
  `5f6be763-42a8-4de6-a497-f59c343aaccb` 上报 `phase=merged`，pending 为空。
- 故障注入仍由 `test_delivery.py`、`test_agent_session_recovery.py`、
  `test_workspace_lease.py` 等单测覆盖，与本条现场验收分开记录。

验收：

- Python unittest/pytest 全绿。
- 原版核心测试映射无未解释项。
- 至少一条真实 Linear + Pi 闭环证据。

## Phase 2：观测与运维

- [x] 明确 `board/` 为官方观测面，或补终端 dashboard。
- [x] 增加领取延迟、运行时长、重试率、阻塞率、outbox 积压、scope 使用量指标。
- [x] 增加阈值告警和运行手册。
- [x] 增加状态备份/恢复、日志保留、依赖不可用降级。
- [x] 增加启动预检和统一运维入口（暂停、释放 Worker、清理工作区）。
- [x] 补齐 MCP scope 容量回收和终止未确认的长窗口恢复。
- [x] 移植原版终端 Status Dashboard。
- [x] 移植原版 CLI guardrail 确认横幅。

观测与运维实现：

- `/api/v1/state` 新增 `metrics` 和 `health.alerts`；
  `metrics` 覆盖 dispatch latency、runs、retries、blocked rate、outbox、
  MCP scope 和 termination_unconfirmed。
- `/api/v1/control`、`pause`、`resume`、`release/{identifier}`、
  `cleanup/{identifier}`；本地回环免认证，远程写操作使用 `server.api_token`。
- 停止请求一发出就撤销 MCP bearer，进程终止确认只影响 claim/slot；
  终止未确认项在每次 poll 重探，30 秒节流，确认后立即释放。
- `python -m symphony --check` 增加启动预检：工作区不可写为致命错误；
  Pi 命令缺失、Agent Session OAuth 缺失、未配置 API token 为能力降级。
- 运行手册见 `docs/operations.md`。
- `state_backup.py` 周期备份 `_delivery` 与 `_agent_sessions`：
  默认 `<workspace.root>/_state_backups`、每小时一次、保留 24 份；
  先写临时文件再原子替换，正常停止时补一次备份。
  `--restore-state <zip>` 校验路径与 JSON 后恢复；备份失败通过
  `state_backup_failed` 告警暴露，不阻断调度。
- `status_dashboard.py` 移植原版终端 UI：Agents、Throughput、Runtime、
  Tokens、Rate Limits、Project、Dashboard、刷新倒计时、Running 和
  Backoff queue。5 个原版快照（idle、idle+dashboard url、super_busy、
  backoff_queue、credits_unlimited）逐字节一致；默认仅在 TTY 或
  `SYMPHONY_DASHBOARD=1` 时启用。
- `cli.py` 增加原版 guardrail 确认参数和红色提示框；缺少参数时退出 1，
  `--check` 与 `--restore-state` 不要求确认。

日志保留已实现（10 MB × 5）。依赖不可用降级已由现有轮询、outbox 和
delivery backoff 覆盖。

最终回归（2026-11-14）：Python unittest 538 项通过（6 skipped），pytest
620 项通过（6 skipped、4 subtests），board 50 项通过，`compileall` 和
`git diff --check` 通过。E2E 服务重启后 `health.status=healthy`，
`metrics` 正常输出，`/api/v1/control` 的 pause/resume 已现场验证。

## Phase 3：外围适配

- [x] Jira Tracker + `jira_rest` 工具 + adapter 测试。
- [x] Asana Tracker + `asana_api` 工具 + adapter 测试。
- [x] GitLab Tracker + `gitlab_api` 工具 + adapter 测试。
- [x] SSH Worker：workspace、hooks、cleanup、agent runner、host 并发。
  远程 MCP 使用 SSH 反向隧道；远程 lease 放在宿主本地状态目录。
- [x] GitHub Tracker 收敛（2026-11-14）：adapter 改为校验状态语义后委托
  `client.py`，分页、ID 刷新、404 跳过、malformed 丢弃与错误传播与原版
  一致；API 失败不再静默为空轮询。`test_github_adapter.py`（7 项）覆盖
  状态校验、分页过滤、顺序、404 和 500 错误传播。
- [x] 跨域重定向凭据隔离（2026-11-14）：新增 `http_client.py`
  `SafeRedirectHandler`，跨域重定向剥离 `Authorization`/`Private-Token`/
  `Cookie`，同域保留。四个 Tracker（GitHub/Jira/Asana/GitLab）共用它；
  `test_http_client.py`（3 项）用本地双服务器验证。
- [x] 对应 live e2e 测试的 Python 等价测试（2026-11-14）：新增
  `test_live_e2e.py`（5 项，默认 skip）：GitHub/Jira/Asana/GitLab 各一个
  真实读取往返，SSH 目标解析 + 参数构造。对应原版 `*_live_e2e_test.exs`
  形状；真实写往返需配置凭据后手动启用。

## Phase 4：退役 Elixir 执行链

- [x] 默认入口、CI、发布物、文档切换到 Python。
- [x] 确认 Elixir 不再参与产品运行。
- [ ] 单独决策 `elixir/` 归档或删除。
- [x] 更新能力矩阵，所有项有最终结论。

默认入口和文档：

- 根 `README.md` 指向 Python + Pi 产品运行方式，推荐
  `deploy/WORKFLOW.local.md`；`elixir/` 明确为上游参考。
- `python/README.md` 的运行和预检示例改用 `deploy/WORKFLOW.local.md`。
- `python-tests.yml` 是产品 CI；`make-all.yml` 仅保留上游 Elixir 行为基线。
- Burrito nightly 和 tag 自动发布已移除，仅保留手动 `workflow_dispatch`；
  Elixir 不再自动产出产品发布物。

`elixir/` 归档或删除仍需单独决策，当前保留用于上游行为对照。

## 当前阻塞与风险

- Linear 人工消息/停止信号尚未回传 Pi。
- MCP scope 上限 1024，长期终止未确认可能耗尽。
- 终止未确认项只在 stall 超时或 Issue 离开活跃状态后重探。
- 远程 Worker 的 `stop_session` 只确认本地 SSH 客户端死亡；远程 Pi 依赖 SSH
  断连后的 SIGHUP，未做远程进程扫描，与原版“关闭 SSH 端口”边界一致。
- 远程 MCP 反向隧道使用与宿主 bridge 相同的远程端口；端口冲突时
  `ExitOnForwardFailure=yes` 会让 SSH 退出，属于 fail-closed。
