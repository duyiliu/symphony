# GitHub 工作流（agent / needs-human）

此功能默认关闭，仅作用于 GitHub Tracker。示例见 [`../WORKFLOW.github.md`](../WORKFLOW.github.md)。
原有 Linear Workflow 与 `required_labels` 的 AND 语义不变。

目标：**默认无人值守完成 Issue**——开发 → 交付 → 宿主确定性合并判定 → 合并 → 关闭。
只有高风险、不明确或无法自动收敛的情况才暂停并请求人工介入。

## 标签语义

| Issue 标签 | 语义 |
| --- | --- |
| `agent` | 唯一常规执行开关：自动开发、返工、检查与合并；执行中持有直到成功或交给人工 |
| `needs-human` | 人工待办标记；暂停执行，必须移除后重新加 `agent` 才恢复 |
| `high-risk` | 人工声明高风险：合并前必须有当前 head 的有效 Review Approval |
| `merge-approved` | 已移除的历史标签，无任何语义（出现即忽略，可清理） |

- `agent` 与 `needs-human` 同时出现一律 fail closed：不派发并记录异常日志。
- 无执行标签不派发；Closed Issue 不再启动任务。
- 人工恢复执行（移除 `needs-human`、重加 `agent`）**不等于合并授权**；合并前宿主重新核验
  当前 SHA 的检查与有效 Review Approval。PR head 变更、审批撤销/过期都会重新核验；新周期首次
  准入时宿主发布恢复评论，记录操作者与上次暂停原因。

## 核心流程

1. Open Issue + `agent` → Symphony 领取；读取 Issue、Workpad 和已有 PR，优先复用同一活动 PR。
2. Agent 开发、测试、自检或按反馈返工，推送 PR，调用 `github_workflow(action="deliver")`。
   宿主解析唯一 closing association（如 `Closes #123`），校验同仓库、非 Draft、目标分支正确，
   冻结实际 head SHA 并发布交付评论（测试文字是 Agent 报告，不是宿主独立执行的证明）。
   **交付后不移除 `agent`**，同一 worker 继续完成合并。
3. Agent 调用 `github_workflow(action="merge")`，宿主做确定性合并判定（不依赖模型声明"安全"）：
   - 最新 head SHA、目标分支、唯一 closing association 与交付记录一致；
   - 当前 SHA 已报告的 GitHub checks 和 statuses 均通过（未完成 → `waiting_checks`；失败 → `needs_repair`）；没有检查时不单独阻止合并，必需检查由仓库分支保护配置保证；
   - GitHub `mergeStateStatus=CLEAN`、`reviewDecision` 不是 REVIEW_REQUIRED/CHANGES_REQUESTED
     （仓库已有分支保护与强制审核仍然生效；UNKNOWN 等待，DIRTY/BLOCKED/CHANGES_REQUESTED 走返修）；
   - 风险规则（见下）；命中规则或 `high-risk` 标签时，必须有该 PR **当前 head 上的有效
     GitHub Review Approval**（User 类型、非作者、commit_id 等于交付 SHA），否则 `waiting_review`。
4. 判定通过 → 宿主执行 squash 合并（请求携带交付 SHA），确认真实合并后关闭 Issue、清理标签。
   合并前保存 `pending_merge` 意图；结果不确定时先查 GitHub 实际状态，绝不重复合并或错误关闭。
5. CI 失败/冲突/review 意见 → `needs_repair`：Agent 修复同一 PR，推送新 SHA 后重新 `deliver`
   （新 SHA 自动取代旧交付，不存在"旧授权绑定新 head"），重新进入合并监测；
   同类失败受 `max_attempts` 预算约束，收敛不了则暂停。
6. 需要人工介入时（Agent 主动 `pause`、预算耗尽、宿主无法核验）：宿主写明原因的评论，
   添加 `needs-human` 并移除 `agent`，receipt 进入 blocked；标签操作失败不会造成派发死循环。

## 风险规则（第一版：确定性路径规则，无 AI 评分）

`tracker.github_workflow.risk`：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `review_required_paths` | 内置保守列表 | glob 列表；命中的文件变更必须有有效 Review Approval 才能自动合并 |
| `max_auto_files` | 100 | diff 文件数超过即暂停转人工（兜底"不可判定"） |

内置默认路径涵盖 DB migration/SQL、`.github/workflows`、deploy 目录、Dockerfile/compose、
认证与权限相关命名、security 目录。路径规则是保守启发式，不能代替语义检查；
人工可随时加 `high-risk` 标签声明高风险；无法核验（如 diff 超预算）默认转人工。

自动合并不要求每个 PR 人工授权，但绝不绕过仓库已有强制审核；PR 上未处理的阻塞性反馈不能跳过。
合并权限仍在宿主/凭据层收敛：`github_api` 禁止改标签、直连 merge、关 Issue、改保护规则、提交 review。

## 配置

`tracker.github_workflow`：

| 字段 | 默认 / 要求 |
| --- | --- |
| `enabled` | 默认关闭，显式设为 true 启用 |
| `state_dir` | 宿主 JSON 记录绝对目录，不与 Workspace 重叠 |
| `base_branch` | 必填，预期 PR 目标分支 |
| `max_attempts` | 默认 3，限制同一标签事件的 worker 准入次数 |
| `risk` | 可选，见上表 |

执行标签（`agent` / `needs-human` / `high-risk`）不要放进 `required_labels`；额外标签仍全部匹配。
Pi 使用宿主工具需要启用 `runtime.mcp.enabled: true`。不再加载解释旧授权评论的 land skill。

## 受信任部署约定

本实现按受信任环境运行，复用仓库现有标签写权限和 GitHub 凭据。
不要求独立自动化账号、Agent sandbox 或额外审批人名单；也不要求事先给未保护分支新增保护规则。
宿主检查已报告的 CI 和 GitHub 合并状态，不主动绕过、修改或移除已有审核与分支保护规则。

通用 `github_api` 禁止添加授权标签、直接 merge、关闭 Issue 和仓库管理等操作，最终合并走宿主入口。
这约束了该工具的调用，**不是对 Agent shell 或共享凭据的强隔离**。
共享身份时不能从 GitHub 事件区分人工与 Agent；Agent 不自行授权依赖 Workflow 约定和运行环境的信任。
不应将这些约定描述为宿主能够从权限上阻止所有自授权路径。

## 重试与中断恢复

不新增数据库、后台服务或调度器。每个 Issue 使用 `state_dir/<number>.json` 保存交付、事件周期、预算、
失败原因、暂停标记及合并意图。采用同步写临时文件再 rename，目录 0700、文件 0600；仅供一个实例独占。

- worker 启动准入计入预算；交付后 worker 中断会在预算内再次派发收尾合并，
  预算耗尽即暂停转人工。重启和普通评论不清零预算；同一周期重复发布不会重复评论。
- 暂停后加/摘标签失败，也会进入现有 blocked 集合，停止续跑并释放运行名额。
  轮询检查新标签事件，读取失败保持暂停；新人工事件可以开启新周期，宿主在新周期首次准入时
  发布恢复评论（操作者、上次暂停原因，并声明恢复不是合并授权）。
- merge 请求前先保存 `pending_merge`。结果不确定时，下次调用先查询真实 PR 状态，不重复发 merge。
  待核实的合并记录不能被新交付覆盖；未确认合并则暂停交给人工。
- 已关闭 Issue 不在候选集合中，其未完成收尾不会后台自动重放。管理员核对 GitHub 和 JSON 后，
  可在宿主调用 `Workflow.execute(settings, %{"action" => "merge", "issue" => "123"})` 完成收尾。
- 不要删除 JSON 来重置预算或在活跃任务中更换记录目录。

## 迁移（从旧两标签协议升级）

1. 先处理完或暂停在途任务。
2. 部署新二进制与 Workflow。旧 `merge-approved` 标签不再有任何语义，可直接移除；
   带 `merge-approved` 等待合并的 Issue 需要重新加 `agent`，按新协议重新交付并走自动合并判定。
3. 旧 receipt 无需转换：receipt 中旧的 `previous_approvals` 等字段不再被读取；
   重新加 `agent` 开启新周期（新周期重置预算与交付），同一 SHA 重新 `deliver` 即可。
4. 看板的“批准合并”入口已移除，改为“转人工处理 / 恢复执行”。

回滚：换回旧版本二进制与旧 WORKFLOW 配置即可；新协议产生的 receipt 中 `blocked`/`merged`
字段语义与旧版一致，旧版本会忽略 `needs-human` 相关状态。

```bash
cd elixir
mix test test/symphony_elixir/github_workflow_test.exs test/symphony_elixir/github_workflow_integration_test.exs
mix lint
make all
```

离线测试覆盖标签路由、预算/暂停、合并判定与风险规则（模拟 GitHub，不发送真实请求）。
需要真实 GitHub E2E 验证的部分：标签事件与 closing association 解析、CI check 拉取、
Review Approval 有效性、真实 squash 合并与 Issue 关闭。建议在测试仓库用一个低风险 Issue
（只加 `agent`）走完整流程，再用一个命中风险路径的 Issue 验证 `needs-human` 暂停与恢复。
