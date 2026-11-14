# Symphony 运行手册（Linear + Pi）

本文覆盖 Python 核心的日常观测、告警和运维入口。`board/` 是官方可视化
观测面，`/api/v1/state` 是机器可读事实源。

## 观测面

```powershell
Invoke-RestMethod http://127.0.0.1:18331/api/v1/state
Invoke-RestMethod http://127.0.0.1:18331/api/v1/control
```

终端前台运行且 stdout 为 TTY 时，会显示原版 Status Dashboard
（Agents、Throughput、Runtime、Tokens、Rate Limits、Project、Running、
Backoff queue）。隐藏进程或非 TTY 默认关闭；需要强制开启时设置
`SYMPHONY_DASHBOARD=1`，需要关闭时设置 `SYMPHONY_DASHBOARD=0`。

`/api/v1/state` 的关键字段：

- `health.status`：`starting`、`healthy`、`degraded`、`stale`、`stopped`。
- `health.alerts`：阈值告警列表，每项含 `code`、`severity`、`message`。
- `metrics.dispatch`：派发次数、平均/最近领取延迟（毫秒）。
- `metrics.runs`：完成数、失败数、当前活跃数、平均运行时长。
- `metrics.retries`：执行重试和控制面延迟次数。
- `metrics.blocked`：累计/当前阻塞数。
- `metrics.rates`：每次派发对应的重试率和阻塞率。
- `metrics.termination_unconfirmed`：终止未确认、仍占用 claim/slot 的数量。
- `metrics.outbox`：Agent Session 队列和持久化 outbox 积压。
- `metrics.mcp_scopes`：MCP bearer scope 使用量、上限和利用率。

## 告警阈值

| code | 级别 | 条件 |
|---|---|---|
| `scheduler_stopped/stale/degraded` | critical/warning | 调度线程停止、快照过期或轮询失败 |
| `polling_error` | warning | 最近一次 Linear/GitHub 轮询失败 |
| `termination_unconfirmed` | warning | 存在终止未确认的 Worker |
| `mcp_scope_pressure` | warning | scope 利用率 ≥ 90% |
| `agent_session_queue_backlog` | warning | 队列 ≥ 200 |
| `agent_session_outbox_backlog` | warning | 持久化 outbox ≥ 100 |
| `agent_session_reporter_down` | critical | Agent Session 线程未运行 |
| `delivery_thread_down` | critical | 宿主交付线程未运行 |
| `delivery_deferred` | info | 交付因限流或门禁延迟 |

阈值定义在 `python/symphony/http_api.py` 顶部，调整后重启服务生效。

## 运维操作

本地回环地址无需认证；远程调用必须带 `Authorization: Bearer <server.api_token>`。

```powershell
# 暂停领取新任务；运行中的 Worker 继续执行
Invoke-RestMethod -Method Post http://127.0.0.1:18331/api/v1/control/pause

# 恢复调度
Invoke-RestMethod -Method Post http://127.0.0.1:18331/api/v1/control/resume

# 释放终止未确认的 Worker；失败返回 409
Invoke-RestMethod -Method Post http://127.0.0.1:18331/api/v1/control/release/YIL-182

# 清理没有活跃运行的工作区；失败返回 409
Invoke-RestMethod -Method Post http://127.0.0.1:18331/api/v1/control/cleanup/YIL-182
```

`release` 只处理 `termination_unconfirmed=true` 的条目；正常运行的 Worker
必须通过 Linear 状态变化或宿主停止流程结束。`cleanup` 在 Issue 仍处于
running/retrying/blocked 时返回 `issue_busy`。

## 备份与恢复

需要备份的本地状态：

- `delivery.state_dir`：交付记录 JSON，默认位于工作区根目录下的 `_delivery`。
- Agent Session 状态目录：默认位于工作区根目录下的 `_agent_sessions`。
- MCP scope 只在内存中，重启后由新 Worker 重新签发；不需要备份。
- 工作区目录：可重建，但包含 Agent 尚未提交的改动，清理前必须先确认。

服务会自动把上述两个状态目录打包为 zip：

- 默认目录：`<workspace.root>/_state_backups`。
- 默认周期：`server.state_backup_interval_ms`，默认 3,600,000 毫秒（1 小时）。
- 保留份数：`server.state_backup_keep`，默认 24 份。
- 自定义目录：`server.state_backup_dir`；把
  `server.state_backup_interval_ms` 设为 `0` 可关闭自动备份。
- 压缩包只包含 `.json` 状态记录，排除 `.tmp.*` 与 `.owner.json`；
  先写临时文件再原子替换，备份失败不会阻断调度。
- 服务正常停止时会再执行一次备份。`/api/v1/state` 的
  `state_backup` 字段显示最近成功时间、失败原因和备份文件数；
  持续失败会产生 `state_backup_failed` 告警。

恢复前必须先停止服务，再执行：

```powershell
python -m symphony --workflow deploy/WORKFLOW.local.md --restore-state <archive.zip>
```

恢复会校验归档路径与每个 JSON 对象，只覆盖 `delivery/` 和
`agent_sessions/` 前缀的文件；损坏或多个未知前缀的归档会被拒绝。

恢复顺序：

1. 恢复 `_delivery` 和 `_agent_sessions` 目录。
2. 启动服务，确认 `/api/v1/state` 的 `health.status=healthy`。
3. 检查 `agent_sessions.pending_events` 和 `delivery.deferred_count` 是否归零。
4. 对 `merged_pending_sync` 记录触发一次 refresh，确认 Linear 状态被核对。

## 日志保留

日志默认写入 `<logs-root>/log/symphony.log`，UTF-8，10 MB × 5 轮转。
可用 `--log-file` 或 `--logs-root` 覆盖。日志中的
`token`/`authorization`/`api_key`/`password`/`secret` 和 `Bearer` 会被脱敏。

## 依赖不可用时的行为

- Linear 轮询失败：保留上次快照，`health.status=degraded`，下个周期重试。
- GitHub 交付失败：记录 `delivery` deferred/backoff，不阻塞调度。
- Pi 启动失败：按控制面策略重试；本地永久错误进入 blocked，等待人工处理。
- Agent Session 上报失败：进入持久化 outbox，恢复后补发并去重。
- MCP scope 达到上限：拒绝新 Worker（fail-closed），不会静默降级。

## 启动预检

```powershell
python -m symphony --check --workflow deploy/WORKFLOW.local.md
```

预检区分两类结果：

- 致命错误：工作区根目录不可写等，返回码 2，不启动。
- 能力降级：Pi 命令不在 PATH、Agent Session OAuth 缺失、未配置
  `server.api_token`，只输出 warning，仍可启动。

正常运行必须显式确认无人值守执行：

```powershell
python -m symphony --i-understand-that-this-will-be-running-without-the-usual-guardrails --workflow deploy/WORKFLOW.local.md
```
