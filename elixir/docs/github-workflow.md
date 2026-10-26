# GitHub 两标签工作流

此功能默认关闭，仅作用于 GitHub Tracker。官方 Linear 示例 `elixir/WORKFLOW.md` 保持不变；
GitHub 示例见 [`../WORKFLOW.github.md`](../WORKFLOW.github.md)。这是代码中的可选能力，不会自动替换部署中的 Workflow。

## 人工操作

| 操作 | 含义 |
| --- | --- |
| 给 Open Issue 添加 `agent` | 开发或返工，复用原 PR |
| PR 留修改意见，再添加 `agent` | 根据反馈继续开发 |
| 给 Issue 添加 `merge-approved` | 授权合并宿主已经登记的交付版本 |
| 移除 `merge-approved` | 撤销尚未发出的合并请求的授权 |

两个执行标签互斥；都没有时不调度；同时存在时停止执行并记录异常日志。
人工纠正非法组合或失败状态后，应显式移除并重新添加所需标签，产生新的执行事件。
Closed Issue 不再启动任务。初版没有后台恢复器；收尾中断后的处理见下文。

## SHA 绑定与授权证据

1. Agent 使用 `github_workflow` 的 `deliver` 操作交付。宿主通过 GitHub 的显式关闭关联解析 PR，
   要求唯一 Open PR、非 Draft、同仓库、目标分支正确，并读取真实的完整 head SHA。
2. 宿主先同步写入交付记录，再发布含 PR、SHA、目标分支和测试证据的交付评论，最后移除执行标签。
   测试文字是 Agent 报告的证据，不是宿主独立运行测试的证明；合并前 CI 另行检查。
3. 交付评论发布成功后，宿主重新读取完整分页的标签事件，保存事件边界和 `published: true`。
   人工添加 `merge-approved` 后，宿主要求交付已发布，且该授权事件不属于发布完成时已存在的事件。
   发布前或发布过程中添加的标签需要人工重新添加；评论发布失败时不能授权。
   因此旧标签不能批准新交付，重复轮询也不会产生新的执行预算。
4. 依赖 GitHub 仓库现有标签写权限，不另建审批人名单；只接受 User 类型、且不同于配置的自动化身份的事件。
   宿主还通过 `GET /user` 确认实际 Token 身份与 `automation_login` 一致。人工和自动化必须是不同身份。
5. 合并前重新检查 Issue、标签事件、PR 关联、head 和目标分支；调用 merge 时传入批准的 SHA。
   新 head、旧标签、撤销、错误关联、缺失记录均不能授权。已发送到 GitHub 的 merge 请求无法靠随后摘标签撤回。

标签是唯一人工授权入口，不要求额外授权评论或单独的 PR Approve。但仓库本身要求的 review 必须满足。
PR 更新后如果宿主已暂停并摘掉授权标签，需要人工加 `agent` 完成新的交付，再重新授权；不会自动登记未经测试的新 head。

## 合并与修复

- 最终 merge 只允许通过宿主 `github_workflow(action="merge")`，始终使用 `squash` 和已登记 SHA。
- 目标分支必须受保护；配置的 `required_checks` 必须非空、存在且成功。
- Check runs 必须属于批准的 head，状态为 completed、结论为 success；commit statuses 必须为 success。
  同名不同来源的检查不能用一个成功结果掩盖其他来源的失败。
- GitHub GraphQL `mergeStateStatus` 必须为 CLEAN；REVIEW_REQUIRED、CHANGES_REQUESTED、未知及阻塞状态不能合并。
- GitHub 端的保护规则仍然是最终防线。宿主拒绝仓库管理员身份；自动化身份还必须不在规则集 bypass 名单中。
  必要时开启分支必须保持最新等保护，不以普通 head CI 代替与最新 base 的兼容性要求。禁止修改规则来完成任务。
- Agent 可在同一个 PR 上修复冲突或 CI，并重新测试；产生新提交后必须重新 `deliver`，撤销旧授权并等待人工重新贴标签。
- 初版仅支持同仓库 PR、明确的 GitHub closing association、非空 CI 检查列表和自动化 machine-user Token。
  GitHub App installation Token、fork PR、merge queue、无 CI 的自动合并不在此实现的支持范围内，不能降级绕过检查。

## 配置

`tracker.github_workflow`：

| 字段 | 默认 / 要求 |
| --- | --- |
| `enabled` | 默认关闭，必须显式设为 true |
| `isolated_agent` | 必须为 true；这是部署者对真实隔离的确认，不会自动创建 sandbox |
| `automation_login` | 必填；独立自动化 GitHub 用户，不得与人工审批用户共用 |
| `state_dir` | 必填绝对目录；每个 Issue 一个 JSON 文件，不能与 Workspace 重叠 |
| `base_branch` | 必填；预期目标分支 |
| `required_checks` | 必填非空列表；填写 GitHub 上准确的 CI context/check 名称 |
| `max_attempts` | 默认 3；每个标签事件的 worker/收尾准入上限，也限制每次交付的交接尝试数 |

`required_labels` 仍然是 AND 语义，用于团队等额外筛选条件。不要把 `agent` 或 `merge-approved` 放进这里；
工作流单独执行二选一判断。初次派发、运行对账、继续执行和重试使用一致策略；切换阶段会结束旧 Agent 会话。

Pi 需要启用现有 MCP bridge（`runtime.mcp.enabled: true`）才能调用宿主工具。
Pi runtime 自身没有 sandbox，必须使用外部隔离运行包装器后才能确认 `isolated_agent`。

## 安全部署前置条件

**不要在当前无隔离的 root Pi 进程上，仅把 `isolated_agent` 改成 true 就认为安全。**

- Agent 不能读取宿主 Token、Workflow 管理文件、合并凭据、ledger，也不能修改宿主源码。
- 隔离环境内不能存在可直接合并/改标签的其他 PAT、SSH 身份、credential helper 或宿主挂载。
- Git push 的凭据也必须受限；不能让 Agent 通过修改有特权的 CI workflow 间接取得宿主凭据或合并权限。
  仓库 CI 权限与 secrets、受保护分支和规则集必须按这一边界配置。
- 仅清除环境变量不足以隔离；`workspace-write` 也不等于隐藏所有宿主可读文件。
- 通用 `github_api` 在本模式下仅允许指定仓库的有限读取、PR 创建/文字更新和评论维护。
  所有标签变更、直接 merge、关闭 Issue、写 review、状态伪造、GraphQL 变更及仓库管理 API 都被拒绝。
- 独立自动化账号必须没有保护规则 bypass 权限；在测试仓库验证后再用于生产。

配置错误、身份不匹配或无法读到保护/授权证据时，必须失败关闭，不能改回 Prompt-only 合并。

## 最小实现的状态与恢复边界

没有新增数据库、GenServer、缓存、后台恢复器或状态 API。现有 Orchestrator 继续负责派发和退避重试。
宿主只在 `state_dir/<issue-number>.json` 中保存交付、标签事件周期、启动次数、最近失败原因、暂停标记和合并意图。
JSON 通过同步写临时文件再 rename 替换；目录权限 0700、文件权限 0600。
同一进程节点内按 Issue 串行执行；一个目录仅供一个 Symphony 实例使用，不支持多个实例共享目录。

- `max_attempts` 限制同一标签事件的 worker 启动数，计数在启动 Agent 前保存，重启不清零。
  每次 run 仍受现有 `agent.max_turns` 限制。没有额外的 CI 总超时服务；长期等待时应调用 `pause`，
  如果 Agent 反复正常退出仍未交付，也会在耗尽启动预算后暂停。交付收尾准入也计数，
  同一 run 中重复调用 `deliver` 的交接尝试另按同一上限限制；已发布评论不会在清理重试时重发。
- 不可用的授权、过期 SHA 等明确错误会保存暂停原因、移除执行标签并发布交接评论。
  暂时性 API 故障返回现有重试流程。两个标签同时存在时不派发；运行中出现时终止该任务。
  该非法组合保留在 GitHub 标签上，路由检查会记录警告日志。
- 交付记录先落盘，再发评论和摘标签。中断后若同一事件仍可调度，下次准入会再次完成交付交接；
  评论可能重复。摘标签前会核对执行事件，避免旧交接清掉已经可见的新信号；
  GitHub 标签接口没有事务，事件核对与摘标签之间仍有短暂竞态。没有承诺外部操作恰好执行一次。
- merge 请求前先保存 `pending_merge`。响应丢失时，下次准入或 `merge` 调用先读取 GitHub 真实状态，
  不重复发送 merge。确认为相同 head/base 已合并才关闭 Issue；否则暂停并要求人工检查。
- GitHub 通常会按 PR 的 closing association 自动关闭 Issue，因此合并后可能已经不在候选集合中。
  此时标签清理、关闭 Issue 等未完成操作**不会自动在后台重放**。管理员需检查 GitHub 和 JSON，
  再从宿主显式调用 `Workflow.execute(settings, %{"action" => "merge", "issue" => "123"})` 完成收尾。
  相同已完成记录重复调用只返回 `merged`。
- 暂停保存成功而摘标签失败时，同一事件仍被本地 `blocked` 阻止。Runner 返回独立的等待结果，Orchestrator 使用已有
  blocked 集合停止续跑并释放运行名额；轮询只校验标签事件，读取失败时保持暂停。人工修正标签并
  重新添加会产生新周期并解除阻塞，普通评论不会解除阻塞。不要删除 JSON 来重置重试数，也不要在任务活跃时切换目录、仓库或身份。
- 这份记录供宿主内部使用；本阶段不新增 Dashboard 的工作流状态聚合、跨实例锁或自动迁移工具。

这些是第一版明确保留的人工恢复边界。它保证授权与 SHA 校验，但不提供完整无人值守故障恢复。

## 从旧看板审批迁移

1. 先暂停派发，处理完或明确停止旧合并任务；不要在活跃会话中热切换授权协议。
2. 当前看板源码已经删除审批/标签写入口，旧 POST 返回 410；重启看板服务才会替换旧进程中的代码。
3. 移除部署 Workflow 中基于 `symphony-merge-authorize` 评论合并的旧规则；旧评论不转换为新授权。
4. 创建 `agent` 和 `merge-approved` 两个 Label，配置仓库保护、独立自动化身份、隔离和私有 JSON 目录。
5. 根据 `WORKFLOW.github.md` 配置真实仓库、分支、CI 名称和隔离启动命令，确认后设置 `isolated_agent: true`。
6. 构建并重启 Symphony，先在测试 Issue 上完成开发交付，再人工添加 `merge-approved` 验证合并及中断后的人工恢复。

不要把当前未验证的线上旧任务自动迁移成授权任务。本次源码修改不会自动替换 `/root/symphony/config/WORKFLOW.md`。

## 验证

```bash
cd elixir
mix test test/symphony_elixir/github_workflow_test.exs test/symphony_elixir/github_adapter_test.exs
mix specs.check
make all
```

测试使用模拟 GitHub 服务，不发送真实标签、评论或 merge 请求。
