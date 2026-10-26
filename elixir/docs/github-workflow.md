# GitHub 两标签工作流

此功能默认关闭，仅作用于 GitHub Tracker。示例见 [`../WORKFLOW.github.md`](../WORKFLOW.github.md)。
原有 Linear Workflow 与 `required_labels` 的 AND 语义不变。

## 人工操作

| 操作 | 系统行为 |
| --- | --- |
| Open Issue 添加 `agent` | 开发或返工，复用关联 PR |
| PR 留意见后重新添加 `agent` | 根据反馈修改并重新交付 |
| 添加 `merge-approved` | 授权合并已发布的交付 SHA |
| 移除 `merge-approved` | 撤销尚未发出的合并请求 |

两个执行标签互斥；都没有时等待人工；同时存在时停止执行并记录异常日志。
Closed Issue 不再启动开发或合并任务。

## 交付与合并

1. Agent 更新 Workpad，记录 PR、测试命令和结果，再调用 `github_workflow(action="deliver")`。
2. 宿主解析唯一的 GitHub closing association（如 `Closes #123`），校验同仓库、非 Draft、目标分支正确，
   保存实际 PR head SHA。测试文字是 Agent 报告，不是宿主独立执行测试的证明。
3. 交付评论发布成功后，宿主重新读取标签事件，保存事件边界和 `published: true`，再移除执行标签。
   发布失败不能授权；发布前或发布过程中添加的授权标签需要人工重新添加。
4. 人工审核并添加 `merge-approved`。宿主检查真实标签事件及 User 类型操作者，要求事件晚于交付发布边界。
5. `github_workflow(action="merge")` 再核对 Issue、Label、关联 PR、目标分支和 SHA，检查配置的 CI，
   以及 GitHub `mergeStateStatus=CLEAN` 和 review gate。未知、冲突、必要审核未满足等状态不能合并。
6. 最终合并使用 `squash`，请求携带获批 SHA。确认相同 head/base 的 PR 已合并后才关闭 Issue、清理标签。
   GitHub 也可能通过 closing association 自动关闭 Issue。

Agent 可以修复冲突和 CI，但新提交必须重新 `deliver`，移除旧授权并等待人工重新贴标签。
不会把旧授权自动绑定最新 head。授权评论和看板按钮不再是审批入口。

## 配置

`tracker.github_workflow`：

| 字段 | 默认 / 要求 |
| --- | --- |
| `enabled` | 默认关闭，显式设为 true 启用 |
| `state_dir` | 宿主 JSON 记录绝对目录，不与 Workspace 重叠 |
| `base_branch` | 必填，预期 PR 目标分支 |
| `required_checks` | 必填非空列表，GitHub 实际 check/context 名称 |
| `max_attempts` | 默认 3，限制同一标签事件的 worker/收尾准入和每次交付的交接尝试 |

执行标签不要放进 `required_labels`；额外标签仍全部匹配。
Pi 使用宿主工具需要启用 `runtime.mcp.enabled: true`。不再加载解释旧授权评论的 land skill。

## 受信任部署约定

本实现按受信任环境运行，复用仓库现有标签写权限和 GitHub 凭据。
不要求独立自动化账号、Agent sandbox 或额外审批人名单；也不要求事先给未保护分支新增保护规则。
宿主仍检查配置的 CI 和 GitHub 合并状态，不主动绕过、修改或移除已有审核与分支保护规则。

通用 `github_api` 禁止添加授权标签、直接 merge、关闭 Issue 和仓库管理等操作，最终合并走宿主入口。
这约束了该工具的调用，**不是对 Agent shell 或共享凭据的强隔离**。
共享身份时不能从 GitHub 事件区分人工与 Agent；Agent 不自行授权依赖 Workflow 约定和运行环境的信任。
不应将这些约定描述为宿主能够从权限上阻止所有自授权路径。

## 重试与中断恢复

不新增数据库、后台服务或调度器。每个 Issue 使用 `state_dir/<number>.json` 保存交付、事件周期、预算、
失败原因、暂停标记及合并意图。采用同步写临时文件再 rename，目录 0700、文件 0600；仅供一个实例独占。

- worker 启动与交付收尾准入计入预算；同一 run 重复交接也有上限。重启和普通评论不清零预算。
- 交付发布成功后，摘标签重试不会重复发布评论。发布结果不确定时评论可能重复，但不会因此获得授权。
- 暂停后摘标签失败，也会进入现有 blocked 集合，停止续跑并释放运行名额。
  轮询检查新标签事件，读取失败保持暂停；新人工事件可以开启新周期。
- merge 请求前先保存 `pending_merge`。结果不确定时，下次调用先查询真实 PR 状态，不重复发 merge。
  待核实的合并记录不能被新交付覆盖；未确认合并则暂停交给人工。
- 已关闭 Issue 不在候选集合中，其未完成收尾不会后台自动重放。管理员核对 GitHub 和 JSON 后，
  可在宿主调用 `Workflow.execute(settings, %{"action" => "merge", "issue" => "123"})` 完成收尾。
- 不要删除 JSON 来重置预算或在活跃任务中更换记录目录。摘标签接口没有事务，事件核对与删除之间仍有短暂竞态。

## 迁移与验证

先处理完或暂停旧任务，停用看板审批和旧 `symphony-merge-authorize` 规则，再部署新二进制和 Workflow。
已有旧评论不能转为新授权；已有 PR 需要完成新的交付登记后才能用标签授权。
一仓库、一 Workflow、一 Symphony 实例，不增加多项目调度。

```bash
cd elixir
mix test test/symphony_elixir/github_workflow_test.exs test/symphony_elixir/github_workflow_integration_test.exs
mix lint
make all
```

测试不发送真实 GitHub 评论、标签或合并请求。
