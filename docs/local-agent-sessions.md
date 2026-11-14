# 本机运行与 Linear Agent Session

本机模式保持原有职责：Linear 管任务，Pi 管实现，宿主管交付。
Agent Session 是可选的进度镜像，其失败不会阻止任务执行或改变合并门禁。

## 启动

使用 `deploy/local.env.example` 在应用目录创建 `env`，准备 Pi 配置和测试仓库的
Contents-only deploy key。不要把真实配置、密钥或 token 放进 Git。

`deploy/WORKFLOW.local.md` 是宿主自动交付示例；复制到应用目录的 `WORKFLOW.md`。
它把 Human Review/Merging 留给宿主，不使用旧模板的 worker `land` 流程。
现有本机 Workflow 的完整正文可以保留，只需增加 `agent_sessions` 配置块。

```powershell
& ./deploy/run-local.ps1 -AppRoot D:/symphony-e2e -Check
& ./deploy/run-local.ps1 -AppRoot D:/symphony-e2e -OAuthEnvFile D:/private/linear-agent.env
```

启动脚本使用独占文件锁并检查端口，防止重复领取。Python 直接输出 UTF-8 滚动日志到
应用目录的 `logs/main.log`，不依赖 PowerShell 的 stderr 重定向。

## 认证与活动

- Tracker、Workpad 继续使用 `LINEAR_API_KEY`。
- Agent Session 单独通过 `LINEAR_OAUTH_CLIENT_ID/SECRET` 申请 app actor token；
  token 只缓存于宿主内存，到期前更新，401 时换取一次，429 按退避处理。
- OAuth app 需要开启 client credentials 和 Agent session events。
- OAuth 凭据、GitHub token 与 webhook signing secret 从 Pi 环境中移除。
- worker 全局 Git 配置为隔离文件。HTTPS 代理应写入仓库局部配置或隔离配置；
  修改用户全局代理不会自动作用于 worker。本机示例继续使用 SSH deploy key。
- Session 摘要是最多 255 字符的单行标题，详细结果写入 Activity。
- 关键工具完成事件默认至少间隔 30 秒，每次生命周期最多 20 条；
  不回传 shell 参数、工具原始结果或模型内部推理。

## Worker 安全边界

- 每次 Pi spawn 获得独立、可撤销的 loopback MCP bearer token。`.pi` 目录为
  `0700`，`mcp.json` 为 `0600`。`.pi` 是 symlink/junction 时只移除链接并重建
  私有目录；`.git` 的 symlink/junction/reparse point 被视为不安全工作区并拒绝
  写穿。更新 `mcp.json` 使用随机临时文件、`fsync` 和原子替换。
- Linear 查询与 mutation 使用独立 token scope。mutation 只能作用于 spawn 时
  冻结的 Issue，并经过 GraphQL tokenizer/AST allowlist；资源 attachment 的
  GitHub PR URL 还必须属于配置仓库。
- `github_api` 的 GET 可读取配置仓库；写操作只能创建 head 分支包含当前
  Issue identifier 的 PR，或修改/评论同一仓库中该 identifier 对应的 open PR。
  `symphony` label 是唯一允许的仓库级 label 创建。worker 没有 merge 能力。
- 工作区运行锁位于可删除 worktree 之外，同时使用进程内 map、跨进程 OS 字节锁、
  host/agent PID 身份和 Pi session token 扫描。无法验证的存活状态一律保持 busy。
- Agent Session 和 delivery 状态使用 UUID 临时文件、`fsync` 与原子替换；
  失败路径清理临时文件。MCP HTTP 单请求上限为 1 MiB，畸形、重复、负数或超限
  `Content-Length` 在读取 body 前拒绝。

| 事件 | Activity / 语义 |
|---|---|
| 自动领取 | thought；建立或恢复 Session |
| 文件操作、验证步骤 | 有节流的 action |
| worker 结束 | thought；等待状态对账/交付，不能宣称合并 |
| 有限重试、返工 | thought；沿用 Session |
| 超限阻塞、高风险暂停 | elicitation；需要人工处理 |
| 已确认合并并回写 Done | response；附 PR 与受门禁 HEAD SHA |
| GitHub 已合并但 Linear 未同步 | elicitation；明确部分成功 |
| 任务取消/离开执行范围 | response；明确停止而非合并 |

上报在独立线程执行，pending Activity 持久化于 `state_dir`（默认工作区根目录下
`_agent_sessions`）的身份子目录。子目录由 GraphQL endpoint、项目、OAuth app ID
和 token endpoint 确定；更换身份不会用新凭据重放旧 outbox，轮换同一 app 的
secret 不改变身份。旧版未绑定身份的平铺状态文件会保留，不自动迁移或重放。

重启、热开启或切换状态目录后恢复当前身份的记录；相同交付事件不重复发送。
队列拥塞时只允许丢弃进度，关键生命周期事件进入独立溢出缓冲。宿主在原子保存
合并/暂停结果时同时保存上报信封；即使尚未 staging 就退出，后台也能按原 Session
的运行标记补发，且不会把旧交付结果回放到重新开启的任务生命周期。

交付记录采用先落盘、后推进 Linear 的顺序：GitHub 确认合并后先写入
`merged_pending_sync`，再更新 Done 和写最终 `merged`。轮询会额外扫描这些记录，
即使 Issue 已 Done、离开活跃状态，也能按 Issue ID 重新核验同一 PR/HEAD 并完成
收尾。若用户在同步完成前把任务重开为 Todo/Rework，旧记录会标为
`superseded_pending_sync`，不会把新生命周期改回 Done。新一次 `running` 同时清理
旧生命周期尚未发送的控制事件并创建新 Session。

Session 创建请求响应丢失时，
先按本地持久化的运行 URL 标记查询恢复；Activity 使用预先保存的 UUID，重试前查询确认。
已完成后重新开启的任务创建新 Session。旧 worker 事件不覆盖已合并/暂停结果。

## 观察与边界

`GET /api/v1/state` 保留原有数据，同时增加：

- `health`：调度线程、快照年龄与 healthy/degraded/stale/stopped。
- `polling`：最近轮询开始、完成、成功时间、最近错误及轮询次数。
- `agent_sessions`：上报线程、队列长度、待发送事件数及最近上报结果。

没有新日志不等于调度停转；应以轮询成功时间是否推进来判断。

本轮只使用主动创建 Session 和向外上报的轮询模式。没有开放公网 webhook 接收端；
Linear Agent 面板的消息、停止信号不会自动进入 Pi。当前 OAuth app 的占位 webhook
仍须在以后提供可达入口时替换，不能把该占位地址称为已接入。

官方契约参考：Linear Developers 的 OAuth 2.0 authentication、Developing the
Agent Interaction，以及 `linear/linear` SDK 的 `schema.graphql`（2026-11-13 核对）。

## 本机验收记录

2026-11-13，测试项目 Symphony E2E 中的 YIL-179 完成真实无人值守闭环：

- 18:09:26 自动领取；使用 Magpie `devin/swe-2`、thinking `max`。
- 自动创建 Session `2a9c8d40-952d-4c81-9afd-eb1779091484`，上报领取、
  5 条工具步骤与等待交付；没有直接发送工具参数、输出或模型推理。
- PR `duyiliu/symphony-e2e#18` 只新增 `src/clamp.js` 和 `test/clamp.test.js`，
  三个 CI 检查通过；18:15:21 由宿主自动合并，
  merge commit `5987e8fe5c8593b37e51555ea4884ef64740d9db`。
- Linear 为 Done；18:16:12 发布最终 response，Session 为 complete，
  summary 与 PR 链接相符，本地 pending 为空。没有手工 push 或 merge。
- 对该 Session 做了一次只读恢复查询，按运行 marker 找回原 ID，远端写入为 0。
  这同时验证了真实 API 返回 JSON 编码 externalUrls 的解析。

详细的 API 结果保存在 `work/任务/本机执行与AgentSession完善/e2e-evidence.json`。
这次验收覆盖本机主动领取和上报，不代表公网 webhook、动态模型路由或生产部署已完成。

补齐异常恢复与身份隔离后，另用 YIL-180 验收最终实现：

- 18:39:08 自动领取，Session `87b2c06d-4c33-43f4-90f6-38347ab0ce3a`。
- PR `duyiliu/symphony-e2e#19` 仅新增 `src/isEven.js` 和测试，
  三个 CI 检查通过；18:44:33 自动合并，
  merge commit `ddfd5e49e34034f7e7b00eb5d1c81661b2921f5f`。
- 交付记录包含当前认证 scope 与 Session marker 绑定的恢复信封，
  `session_phase=merged`；18:45:23 最终 response。
- Linear Done、Session complete、恰好一条最终 response，pending 为 0。

API 证据为 `work/任务/本机执行与AgentSession完善/e2e-r01-evidence.json`；
完整 pytest 235 通过、1 项默认未开启的真实 Pi 测试跳过，另有上述真实闭环验收。

合并落盘与生命周期隔离修复后，故障注入覆盖了“Linear 已 Done、最终保存前退出”
和“旧 pending-sync 遇到任务重开”两条路径。当前离线验证为 unittest 151 项
（150 通过、1 跳过）、pytest 238 通过、1 跳过；这不替代公网 webhook 或新的真实
Linear/GitHub 闭环验收。

随后以最新代码在本机完成 YIL-181 真实闭环：19:09:27 自动领取，沿用
Magpie `devin/swe-2`、thinking `max`；19:11:14 创建 Session
`7aecc331-7c38-477d-bad4-78cc63bc7af9`。PR
`duyiliu/symphony-e2e#20` 仅新增 `src/countWords.js` 和测试，CI 与宿主门禁
通过后于 19:15:00 自动合并，merge commit
`5d69d6d238778e62362e5bca0b7e1cc2aa640a50`。Linear 为 Done，Session 为
complete，最终 response 恰好 1 条，本地 pending 为 0。证据见
`work/任务/本机执行与AgentSession完善/e2e-r02-evidence.json`。
