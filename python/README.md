# Symphony（Python 实现）

Symphony 调度器的 Python 版本，与 `elixir/` 中的参考实现语义对齐：轮询
GitHub Issue → 为每个 issue 创建隔离 workspace → 驱动 `pi --mode rpc`
编码代理 → 并发上限 / 指数退避重试 / 对账停跑 / 终态 workspace 清理，
并通过 `/api/v1/state` 向既有 Python 看板（`board/`）暴露运行状态。

设计目标是**可以并存、可以回滚**：本实现不动 Elixir 服务，部署时只切换
systemd 单元。

## 架构映射（Elixir → Python）

| Elixir | Python |
|---|---|
| GenServer（Orchestrator） | `orchestrator.py`：单线程 `queue.Queue` 邮箱循环 |
| Task.Supervisor + Task | 每个 issue 一个 daemon runner 线程 |
| `Process.send_after` | `threading.Timer`（tick / retry backoff） |
| `:DOWN` 监控 | runner 线程退出时向邮箱推送 `worker_done` |
| Erlang Port | `subprocess.Popen` + stdout 读取线程 |
| Phoenix `/api/v1/*` | `http.server.ThreadingHTTPServer` |
| Bandit MCP bridge | loopback `ThreadingHTTPServer` + bearer token |
| `Config.settings!()` | `workflow_store.py`（last-known-good，1s 轮询重载） |

## 目录

```
python/
  symphony/
    workflow.py        # WORKFLOW.md 解析（front matter + prompt）
    config.py          # 类型化配置、默认值、校验、$ENV 间接引用
    workflow_store.py  # last-known-good 缓存 + 变更重载
    issue.py           # Issue 数据模型与 routable 判定
    template.py        # Liquid 子集渲染器
    paths.py           # 路径规范化 / workspace 逃逸防护
    workspace.py       # workspace 创建、hooks、安全删除
    orchestrator.py    # 调度主循环（派发/重试/对账/清理/快照）
    mcp_bridge.py      # loopback MCP bridge（github_api / github_workflow）
    http_api.py        # /api/v1/state、/api/v1/refresh、/api/v1/{id}
    logging_setup.py   # 结构化日志
    cli.py             # 入口
    github/            # GitHub REST 客户端、issue 规范化、workflow policy、
                       # host 托管的标签工作流（receipts）、agent 工具
    agent/             # pi JSON-RPC 运行时、每 issue runner
  tests/
```

## 运行

```bash
cd python
pip3 install -r requirements.txt   # 仅 PyYAML
export GITHUB_TOKEN=...            # tracker.provider.token 引用的变量
python -m symphony --workflow ../deploy/WORKFLOW.md --check   # 仅校验
python -m symphony --workflow ../deploy/WORKFLOW.md           # 启动
```

CLI 参数：`--workflow/-w`、`--host`、`--port`（覆盖 WORKFLOW.md 的
`server.host`/`server.port`）、`--check`、`--log-level`。
环境变量：`SYMPHONY_WORKFLOW`、`SYMPHONY_LOG_LEVEL`、`SYMPHONY_LOG_FORMAT=json`。

## 与 Elixir 版的行为等价说明

已移植的语义：

- `agent`/`needs-human`/`high-risk` 标签路由（`github_workflow` 启用时
  `agent`+`needs-human` 同时存在判 `invalid`，fail-closed 不派发）
- 派发前 `begin_run` 准入（receipt 周期、resume、attempts 上限 → 自动 pause）
- 每 issue 一个 pi 会话；只有 `agent_settled` 结束 turn
- stall 检测（`codex.stall_timeout_ms`）→ 终止 + 退避重试；
  input-required 类事件 → 转 blocked 而非重试
- 正常退出 → 1s continuation 重试（复核 issue 是否仍活跃）
- 异常退出 → 10s 起指数退避（封顶 `max_retry_backoff_ms`）
- 每 tick 对账 running/blocked：终态 → 停跑并清 workspace；不可路由 /
  相位变化 → 停跑释放 claim；消失 → 停跑/释放
- `github_workflow` receipt 状态机（deliver/merge/pause/status）：
  per-issue 锁、`0700` 目录 `0600` 文件、pending_merge 恢复、
  风险分级（review_required_paths / max_auto_files / high-risk 标签）、
  squash merge + 合并确认轮询 + fail-closed pause
- `.pi/mcp.json` 注入 bearer token；`.git/info/exclude` 排除 `.pi/`；
  `.pi` 项目级注入面（extensions/skills/prompts/SYSTEM.md 等）每次启动清空
- 响应结构：`/api/v1/state` 与 Phoenix Presenter 同构，看板零改动

有意收窄（与 Elixir 版差异）：

- 只支持 `tracker.kind: github` 和 `runtime.kind: pi`
- 不支持 `worker.ssh_hosts` 远程执行（配置了会报错而非静默降级）
- 无 Phoenix dashboard / LiveView（看板已覆盖）
- `github_api` 在 `github_workflow` 未启用时不限路径（与 Elixir 相同，
  由 token 权限兜底）
- 模板引擎是 Liquid 子集：不支持过滤器（`|`）与 `{%- -%}` 空白控制；
  Solid 的 `strict_filters` 对未注册过滤器同样报错，当前部署模板未使用
- workspace hooks 额外注入 `SYMPHONY_WORKSPACE` 环境变量（无害超集）
- agent 环境会 unset `secret_environment_names`（`GITHUB_TOKEN` 等）；
  Elixir 侧该列表实际为空、不做 unset，此处有意加固 least-authority
- 依赖仅 PyYAML；目标 Python ≥ 3.10

## 部署（199）

见 `deploy/symphony-python-199.service`（与 Elixir 单元同构：同一个
`/root/symphony/env` 环境文件、同一份 `/root/symphony/config/WORKFLOW.md`、
同一个 8320 端口）。先决条件：Python ≥ 3.10 + `pip3 install pyyaml` +
`pi` CLI 在 PATH。

切换方式（保留回滚能力）：

```bash
sudo systemctl stop symphony      # Elixir 版仍在，文件不动
sudo systemctl start symphony-python
# 回滚：start symphony / stop symphony-python
```

注意两个服务不能同时运行（共享 8320 端口与同一批 issue）。
