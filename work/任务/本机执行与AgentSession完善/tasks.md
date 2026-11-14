# 本机执行与 Agent Session 完善

> 状态：完成
> 设计基线：无；沿用 Issue #14、现有宿主交付与 Agent Session 契约
> 代码基线 branch：feat/linear-agent-sessions
> 代码基线 commit：92cfd879beb95a685d58b6453adc47f5a173218d
> 任务前既有工作区修改：运行代码工作树无修改；主工作树已有 .gitignore 修改及未跟踪目录，保留
> 实现审查：最终针对性复审 PASS（Critical/High=0）
> 实现审查模式：independent
> 实现审查轮次：3/3 + 2 次针对性收敛确认
> Reviewer：Meitner（01a1d469-1226-7883-b2cc-005164f864f6）
> 环境验收：YIL-181 最新代码真实闭环通过；R03 终态冷启动健康

## 范围与验收

- 本机开发和测试；不合并主线、不推送代码、不修改 214、不升级 ego。
- 保持 Linear API key 用于 tracker/Workpad，独立 OAuth app token 仅用于 Agent Session。
- 可观察最近成功/失败的轮询，日志由 Python 直接写文件；不能把空闲或缓存快照误报成完整健康。
- Agent Session 上报不能阻塞调度或放宽交付门禁；失败降级。
- 领取、有限关键进度、返工、暂停及最终交付可追踪；worker 结束不冒充 PR 合并。
- OAuth token 复用、到期换取、认证失败恢复；凭据不进入 worker、源码、日志和 session 状态。
- 继续使用本机 Magpie SWE-2 max 和 SSH deploy key。
- 外网 webhook 接入、模型路由、多项目与生产部署不属于本轮。

## 任务队列

### T01 运行健康和日志

- 状态：done
- 依赖：无
- 目标：核实日志停止写入与历史 TLS 错误，补充真实轮询健康及可靠文件日志。
- 验收：状态 API 区分最近轮询成功、失败、时间与调度线程状态；失败不停止后续轮询；本机文件日志可复盘。
- 验证：确定性单测；本机连续轮询和刷新。
- 已有证据：29732 的 refresh 请求立即响应；当前独立 Linear 查询成功；日志最后记录 16:03 TLS handshake FileNotFoundError，尚不能认定调度卡死。
- 完成证据：新服务 PID 24940（18:22 重启后为 30740），Python 直接写 UTF-8 滚动日志；轮询成功时间与次数持续推进，`health/polling` 单测通过。未声称修复历史 TLS 根因或覆盖完整 delivery 健康。

### T02 独立 OAuth 认证

- 状态：done
- 依赖：无
- 目标：Agent Session 使用 client_credentials，缓存 token，并处理失效与限流。
- 验收：不改 tracker 身份；并发复用 token；错误不泄露凭据；运行时剥离 OAuth 凭据。
- 验证：认证成功、到期、401、429、网络失败和凭据隔离回归。
- 完成证据：独立 OAuth viewer 为 Symphony Agent；真实 Session 自动创建成功；缓存、并发、刷新、401、429、凭据剥离测试通过。

### T03 异步生命周期与交付回写

- 状态：done
- 依赖：T02
- 目标：领取、关键进度、重试/返工、暂停、合并结果异步回写 Agent Session。
- 验收：不阻塞调度；节流和去重；重启复用；旧 worker 结束事件不覆盖最终合并；PR 链接和真实结果一致。
- 验证：本地 stub 集成、生命周期和交付门禁回归。
- 完成证据：YIL-179 自动创建 Session、5 条工具 action、等待交付 thought、合并后 response；最终 complete、含 PR 链接、outbox 清空。恢复测试采用真实 JSON 编码 externalUrls，非法响应保持待重试，避免重复创建。

### T04 本机配置与验收

- 状态：done
- 依赖：T01、T02、T03
- 目标：补充可复用部署说明/模板，接入既有本机凭据，重启空闲测试实例并验证。
- 验收：自动领取后自动创建 Session 并报告最终结果；API、Linear、PR、状态文件、日志证据相符。
- 验证：完整 Python 测试、配置校验、本机测试项目真实闭环、只读实现审查。
- 已有证据：2026-11-13 18:09:26 自动领取 YIL-179；18:15:21 PR #18 合并；18:16:12 Agent Session response 完成。全程未手动 push/merge。
- 测试证据：`python -m unittest discover -s tests -q` 140 项，139 通过、1 跳过；`compileall`、`git diff --check` 通过。`run-local.ps1 -Check` 通过。
- 额外回归：`python -m pytest -q --disable-warnings` 227 通过、1 跳过。首次发现历史 allowlist 用例还期待 worker 合并 PR，已将合法合并请求移到拒绝表并验证无 GitHub 调用，未放宽现有策略。
- 启动验收：样例 Workflow 的 `-Check` 通过；重复启动被文件锁拦住并返回 1。本机薄包装补上 `$ErrorActionPreference = 'Stop'`，避免嵌套脚本失败仍返回 0。
- 重启验收：18:22 空闲重启，PID 30740，轮询 healthy，队列为空；原 Session/交付持久化记录保持 merged。恢复路径真实只读查询 1 次找回同一 Session，0 远端写入。
- 真实验收详情：`e2e-evidence.json`；PR https://github.com/duyiliu/symphony-e2e/pull/18；Session `2a9c8d40-952d-4c81-9afd-eb1779091484`。
- 最终验收：YIL-181 在最新代码和进程上自动领取、执行、创建 Session、创建 PR、通过 CI/门禁、自动合并并进入 Done；证明本轮恢复状态机没有破坏正常闭环。

## 完成闸门

- [x] 所有任务完成并记录真实验证
- [x] 确定性回归通过
- [x] 本机环境验收通过或明确真实阻塞
- [x] 实现审查 PASS，Critical/High 为 0

## 审查记录

- 第 1 轮：Critical 0 / High 1 / Medium 3，FAIL。
- High：队列饱和丢终态、网络发送期间内存事件未持久化，重启不能补发。
- Medium：旧 worker blocked 覆盖已合并；认证切换后旧 outbox 未隔离；热开启未恢复旧 pending。

### R01 异常恢复与隔离返工

- 状态：done
- 目标：利用原子保存的宿主交付结果补偿最终上报；仅允许丢弃拥塞的进度，保留生命周期事件；补齐终态保护、按认证配置隔离恢复、热开启恢复。
- 验证：SWE-2 测试子代理新增对抗回归；完整离线回归；原 Reviewer 增量复审。
- 测试责任：Laplace（SWE-2，01a1d475-b097-7cc3-b126-779f02edde13），仅新增异常恢复测试。
- 已实现：批量 staging、控制事件溢出缓冲、宿主交付结果原子携带恢复信封；按运行 marker 防止旧交付结果污染新生命周期；按 OAuth app/endpoint/project 隔离目录并在启用/配置切换时恢复。
- 已验证：新增 8 项对抗测试全部通过；主 Agent 独立复跑 unittest 148 项（147 通过、1 跳过）、pytest 235 通过/1 跳过、compileall、diff 检查通过。
- 最终版环境验收：PID 24776，YIL-180（`2245fda6-14a9-4340-913f-f143443a46c5`）18:39:08 自动领取；PR #19 三个 CI 通过，18:44:33 自动合并（`ddfd5e49e34034f7e7b00eb5d1c81661b2921f5f`）。
- Agent Session `87b2c06d-4c33-43f4-90f6-38347ab0ce3a` 于 18:45:23 完成最终 response。只读 API 确认 Linear Done、Session complete、恰好 1 条 response；恢复信封真实落盘，pending 为 0。详见 `e2e-r01-evidence.json`。

### R02 合并恢复时序与生命周期隔离

- 状态：done
- 第 3 轮新增 High：GitHub merged 后先更新 Linear Done、后写最终本地记录，若中间退出，Done issue 不再进入活跃轮询，恢复信封可能永久缺失。
- 修复：确认 GitHub merged 后先写 `merged_pending_sync`；轮询额外扫描该记录，并通过 Issue ID 查询 Done issue；最终写 `merged` 前完成评论与 Session 收尾。
- 第 3 轮复审再发现：重开任务遇到旧 pending-sync 可被再次改回 Done；修复为校验 `pre_sync_state`/更新时间，旧生命周期降为 `superseded_pending_sync`，同一旧 PR 在连续轮询中持续拒绝，只有新 PR 才重新进入门禁。
- Session 隔离：新一次 `running` 遇到旧合并/pending-sync/superseded 生命周期时，清空旧 pending、Session ID、marker 和 sent keys，创建新 Session，不回放旧控制事件。
- 验证：Laplace 新增故障注入与两次轮询回归；相关 56 项通过；最终审查明确 PASS，Critical/High=0。
- 最终离线验证：unittest 151 项（150 通过、1 跳过），pytest 238 通过、1 跳过，`compileall`、Workflow `-Check`、`git diff --check` 通过。
- 最新真实闭环：YIL-181 自动领取；PR #20 merge commit `5d69d6d238778e62362e5bca0b7e1cc2aa640a50`；Session `7aecc331-7c38-477d-bad4-78cc63bc7af9` complete；Linear Done、恰好 1 条最终 response、pending 0。证据：`e2e-r02-evidence.json`。

### R03 Worker 安全边界与终态幂等

- 状态：done
- 目标：收敛 MCP/Linear/GitHub 的跨 Issue 与跨仓库写入边界，补齐进程终止、租约恢复和持久化状态文件的幂等性。
- `.pi` 防逃逸：拒绝 symlink/junction/reparse point 写穿；私有目录与 `mcp.json` 分别收紧为 `0700/0600`，配置使用 UUID 临时文件和原子替换。
- Worker scope：每次 spawn 使用独立可撤销 bearer；Linear issue/project scope 与 GraphQL AST allowlist fail closed；确认进程树死亡后才撤销 token。
- PR 归属：attachment 只接受配置仓库 PR；delivery gate、PR 创建与后续评论都要求 head 分支按 token 边界包含当前 Issue identifier。GitHub 写操作不得落到其他分支、PR、Issue 或仓库；merge 仍仅属宿主。
- 终止与调度：一次强杀未确认后，后续成功 stop 会恢复 termination confirmed；已退出 runner 的保留项仍可重探测。`thread.start()` 失败使用有界控制面退避，不再无限静默重试。
- 统计幂等：同一 session completion 只累计一次 `seconds_running`，停止保留后 finalize 不重复累计。
- 租约与持久化：运行租约位于 worktree 外，结合 OS 锁、PID 身份和 Pi session token 判定；released marker 写失败仍尝试删除 sidecar，避免同进程重入自锁。Agent Session 与 delivery 记录使用 UUID 临时文件并在失败路径清理。
- HTTP 与评论：MCP 拒绝畸形、重复、负数及超过 1 MiB 的请求长度；交付评论在确认查询失败时保留 pending，避免 response 丢失后重复发送。
- 定向验证：workspace lease / Agent Session / delivery / MCP 共 90 项通过；SWE-2 安全复审最新结论为 Critical=0、High=0，列出的 6 个 Medium 均已对应修复。
- 租约偶发失败修复：完整 `unittest` 首轮暴露 2 个 Windows 进程观测用例失败，根因为 CIM 查询偶尔超过短时确认窗口；将进程观测超时与租约确认窗口分离为 10 秒和 3 秒，并使用唯一 session ID 隔离真实进程测试。修复后完整 `unittest` 连续 3 次通过。
- 完整回归：`unittest` 连续 3 次均为 238 项通过、1 跳过；`pytest` 325 通过、1 跳过、4 个 subtests 通过；`compileall`、`git diff --check` 通过。
- 独立终态回归：DeepSeek 对快照 `925eaad11a383ccf99744dac6c46206a6d95f701d8f68c114e47922ad5cb638a` 给出 Critical=0、High=0；17 项临时逆向测试通过。其发现的恢复夹具 PR 分支不匹配已按真实门禁修正，未放宽生产检查；MCP 401/404 响应已改为 `Connection: close`，避免 Windows 上 keep-alive 残留请求体污染下一请求。
- 部署与冷启动：`deploy\run-local.ps1 -AppRoot D:\symphony-e2e -Check` 通过；09:49:54 冷启动成功，API PID 20784 监听 `127.0.0.1:18331`，轮询与 delivery 轮次持续推进，`health=healthy`，agent session 线程存活，running/retrying/blocked 均为 0，delivery deferred 为 0。日志中 08:35 前的 YIL-167/YIL-169 GitHub 403 是历史记录；本次冷启动后的轮询与 delivery 均为成功。
- 残余风险：`mcp_bridge._State` 的 scope 上限为 1024，达到上限会抛 `RuntimeError("mcp_scope_limit_reached")`；终止未确认的 worker 不撤销 bearer，因此持续异常时可能耗尽 scope。本轮选择保留 fail-closed 和既有回收边界，记录为后续容量/恢复设计项，不扩大 R03 改动。
- 残余风险：终止未确认的 retained entry 在 stall 超时或 Issue 离开活跃态前不会高频重探，恢复窗口较长；当前行为保持 fail-closed，避免租约和进程树未确认时重复执行。

## 阻塞项

- 无。
