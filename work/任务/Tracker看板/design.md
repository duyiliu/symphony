# Tracker 看板：复刻 Symphony 官方 Linear 示例工作流

> 设计版本：v2
> 设计审查：PASS
> 审查模式：independent
> 设计审查轮次：2/3
> 设计审查范围与结论：同一Devin SWE-2 Max子代理独立审查两轮。首轮C1与H1–H4已闭合，第二轮Critical=0、High=0；覆盖官方状态/调度/返工/人工等待、GitHub写通道与凭据、合并前复查及在途请求限制、来源closed可见性、导入时机与锁、迁移PR/workpad关联和pending_merge。未覆盖逐行UI模板、land_watch源码、既有测试逐条差异、真实环境与运行时验收；这些由实现与验证承担。
> 设计确认：已确认
> 设计确认依据：用户明确要求“全部参考 Linear 的方案，以他的为准”“原样照搬先不要扩展”“先出方案”，随后指定“devin swe2 max 来实施，用子代理”。这组指令授权按官方参考收敛并由指定子代理实施，不再就相同方向重复请求确认。
> 审查阶段依据：用户明确改变 v1 核心契约，取消 host 合并和自定义增强，故 v2 新阶段从 0/3 开始。
> 本文件是唯一主设计；执行现状只放 tasks.md。取代本任务 v1 及此前 GitHub 状态收敛方案；不批量回退工作区。

## 1. 目标与参考依据

目标：保留 GitHub issue 作为任务来源，自己实现一个最小 tracker 和看板，复用 Symphony 的调度模型，业务流程逐项遵循官方仓库的 Linear 示例，不购买 Linear SaaS。本期先还原流程，日志、统计、自动返修调度等增强后续另议。

这里的“原样”指 **Symphony 随仓库提供的 Linear WORKFLOW 和相关 land skill**，不声称它是 Linear 产品唯一或固定的流程。Linear 本身允许配置状态；七个主要状态来自此示例。

固定参考版本：本地 upstream/main，commit `be10a1b79df723d6d7612b5651c8522704dafb2e`（本轮读取，非实时远端最新性保证）。

- [官方 Linear 示例 WORKFLOW](https://github.com/openai/symphony/blob/be10a1b79df723d6d7612b5651c8522704dafb2e/elixir/WORKFLOW.md)：Status map、Step 0–4、PR feedback sweep、blocked-access escape hatch、Workpad template。
- [官方 land skill](https://github.com/openai/symphony/blob/be10a1b79df723d6d7612b5651c8522704dafb2e/.codex/skills/land/SKILL.md) 及同目录 `land_watch.py`：worker 等待 CI、处理反馈和冲突、执行合并。
- [官方 SPEC](https://github.com/openai/symphony/blob/be10a1b79df723d6d7612b5651c8522704dafb2e/SPEC.md) 第 7、8、11 章：claim、continuation、重试、对账与 tracker 边界。
- `elixir/lib/symphony_elixir/tracker.ex` 和 `tracker/issue.ex`：通用读取边界与归一化模型。

### 范围

1. 本地任务状态读写、GitHub issue 导入、任务详情及状态看板。
2. 将本地 tracker 接入通用调度接口；agent 使用 tracker 工具读写任务。
3. 移植官方 prompt 协议与 land/相关 skill 的必要依赖。
4. 将现有五态、host 合并实现收敛到本设计，并迁移已有任务记录。
5. 本地确定性验证与受控浏览器验收；不部署 199、不改生产任务、不发送真实 GitHub 评论或合并 PR。

### 必要载体替换

| 官方载体 | 本地替换 | 保持的语义 |
|---|---|---|
| Linear issue | GitHub issue 身份 + 本地任务记录 | 编号、内容来源与工作流状态可查询 |
| Linear 状态字段 | 本地 task.state | 唯一业务状态，不使用标签充当状态 |
| Linear MCP / linear_graphql | 最小 task_api 工具 | agent 查询任务、更新状态、关联 PR |
| Linear workpad 评论 | GitHub issue 上固定 workpad 评论 | 创建、读取、原地更新、返工时删除重建 |
| Linear UI | 本地看板 | 人工查看任务、改状态、写反馈 |
| Codex worker | 现有 Python/pi worker | 工作流与模型无关；本期不重写运行时 |

这些替换是自建 tracker 所需基础能力；不引入 host 自动合并、approved_sha、评论 digest 队列、repair_budget、自动归档或新报表。现有运行日志可以保留展示，不扩展日志产品。

## 2. 工作流契约

### 2.1 状态与执行者

状态名按官方示例输出，不让 `Issue.state` 继续混用 GitHub 的 open/closed。

| 状态 | 调度分类 | 处理方式 |
|---|---|---|
| Backlog | 非活跃 | 新导入的 open issue 默认进入；人工移到 Todo 才执行 |
| Todo | 活跃 | worker 首先改为 In Progress，再创建/恢复 workpad，然后实现 |
| In Progress | 活跃 | worker 继续现有分支、workspace 和 workpad |
| Human Review | 非活跃 | 等待人审；不启动 worker、不后台合并 |
| Merging | 活跃 | worker 执行 land skill，检查/修复/合并，成功后改 Done |
| Rework | 活跃 | worker 按官方完整重做流程处理，不等同于增量修补 |
| Done | 终止 | 停止调度、清理 workspace |
| Closed / Cancelled / Canceled / Duplicate | 终止 | 同官方配置识别并清理；UI 可作为终止状态选项，不需每个独立列 |

配置：`active_states: [Todo, In Progress, Merging, Rework]`；`terminal_states: [Closed, Cancelled, Canceled, Duplicate, Done]`。调度器继续按配置做大小写无关比较。

### 2.2 主链路

```text
GitHub open issue → 本地建档 Backlog
人工：Backlog → Todo
worker：Todo → In Progress → 实现/验证/PR反馈扫描 → Human Review
人工：Human Review → Merging（表示业务认可）
worker：Merging → land（检查、必要修复、合并）→ Done
人工：Human Review → Rework（说明返工原因）
worker：Rework → 重读任务与人类反馈 → 关闭旧PR → 删除旧workpad
        → 从 origin/main 新建分支 → 新workpad/新计划 → 重新实现 → Human Review
```

按官方 Step 4，Rework 启动后可以保留 Rework 状态执行；不强制通过 admission 改为 In Progress。Todo 的状态变化由 agent 工具执行，调度器只 claim，不在 admission 偷换业务状态。

**两种反馈处理必须区分：**

- `Todo` 已有关联 PR：先扫描全部反馈，在原 PR 上修复、验证、重新交 Human Review。
- `Rework`：完整方法重置，关闭旧 PR、重建分支和 workpad。

Human Review 的新 PR 评论本身不会自动拉起 worker。官方 prompt 有“按需查看反馈”的文字，但默认 Human Review 不在 active_states，常驻自动评论监听并非调度器保证。本期由人工改为 Rework 或 Todo 触发；不为消除这项限制增加后台调度。

### 2.3 人工批准与合并

- 人工把 Human Review 改为 Merging，就是流程里的批准；不是独立 `approve` 记录。
- Merging 仍占正常 worker 槽，worker 按 land 等检查、解决冲突、修复 CI、处理反馈，然后合并。
- 业务批准不等于 GitHub PR Review，不替代仓库 rulesets/branch protection；GitHub 拒绝合并时，不绕过规则。
- task_api 是普通 tracker 写工具，不保留“agent 技术上绝不能写 Done”之类原文不存在的权限断言。人工批准这一职责由 prompt 约束，agent 不自行授予批准。
- land 每次继续等待/处理反馈，以及最终提交合并请求之前，必须重读 task，仍为 Merging 才继续；状态已改变就结束当前land路径，按新状态路由，不拿旧revision写Done。采用官方基线的agent复查方式，不新增宿主merge状态门/跨系统锁；已经发往GitHub的请求无法通过看板状态回滚，这个并发窗口需在UI操作提示和文档中如实说明。
- 不运行 HostController，不保留 merge_eval/admission_only/merge_poll 专用旁路，不做低风险自动合并和高风险自定义批准分支。

### 2.4 人工协作与阻塞

遵循官方 workpad 和 blocked-access escape hatch：正常工作不向人反复提问；尝试已记录的工具/认证替代路径仍失败，才在 workpad 写清缺少什么、影响及人工解阻动作，然后转 Human Review。人工补充信息后转 Todo/In Progress 继续执行。

运行时 `input_required`/approval/elicitation 使用调度器原有 blocked 语义（内存中的运行阻塞，不是持久业务状态）。看板展示阻塞原因；任务变成非活跃/终止或不再可路由时释放，重启按官方清空内存 blocked。不新增持久 park 状态、修复预算或自主解阻协议。

如果 task store 本身不可读写，应报告存储错误并停止新派发；不能假装“已转 Human Review”。原 workpad 可写时记录故障，否则保留已有运行日志。不得将损坏记录默认视为 Backlog 后覆盖。

## 3. Tracker、数据与看板

### 3.1 唯一状态和来源边界

GitHub 管编号、标题、描述、原生评论和 PR；本地 tracker 管工作流状态、分支及 PR 关联。看板不另存一套状态。

- 默认来源发现只拉 open issue；新发现任务 → Backlog。首次手工按编号导入 closed issue → Closed，不默认扫描全仓 closed 历史。来源按repo分页，PR记录不作为任务。
- 已建档任务的读取范围是本地记录集，**不受来源open/closed过滤**；按ID刷新发现来源closed仍返回本地task.state，dispatchable不因closed变false。看板显著显示“来源issue已关闭”；本地取消要在看板改Cancelled/Closed。来源404/权限失败按SPEC区分：确认404为不可见并省略，认证/网络等错误返回失败，不能伪造不存在。
- 后续 GitHub open/closed 作为 `native_ref`/来源元数据展示，不能覆盖本地工作流状态；本期不增加外部关闭/重开到 Done/Todo 的自动同步。
- 完成由 worker 在 PR 合并成功后写 Done。PR 外部已合并时，下一次进入 Merging 的 worker 确认事实后写 Done；不增加后台 PR 生命周期同步服务。
- GitHub `Fixes #N` 自动关闭来源 issue 可以保留，它不是第二条本地状态迁移路径。

### 3.2 来源导入时机

服务启动及现有poll tick的来源发现步骤调用同一个import服务：每次按`GET /repos/:repo/issues?state=open&per_page=100&page=N`完整分页，忽略PR；缺记录时持该任务锁再次检查，仅创建Backlog，已有记录仅刷新来源内容缓存，不覆盖业务state。创建调用与人工/agent更新共用同一服务进程和锁。不是新后台控制器，不在看板GET时隐式导入。

已有全部open issue进入Backlog是预期行为，不自动执行。来源列表分页未完整成功时不批量提交本次发现结果，记录错误并保留现有本地任务可读；大仓列表耗时属于沿用官方轮询的已知成本，本期不加增量游标。单个closed历史任务可由同一服务的`POST /api/v1/tasks/import`（issue_number）显式导入。调度按本地已建档任务筛选，来源发现失败不能把已建档任务当成消失。

### 3.3 最小持久化

复用 JSON 原子写；每个配置仓库使用独立 state_dir，记录保留并校验 repo，避免相同 issue number 串仓。多个进程不共享写锁：唯一服务进程拥有写权限，看板和 agent 都经 HTTP/API 写，不让看板直写文件。

记录最少含：schema_version、repo、issue_number、state、revision、branch_name、PR 链接/编号、workpad_comment_id、updated_at。issue 标题/描述可缓存，GitHub 为内容权威。现有原生日志/历史数据保留，但不新增 history 时间线功能。

写操作锁内校验 repo、state 枚举及 revision，原子替换文件。状态、PR/workpad 指针更新同时递增 revision；过期更新返回冲突，调用方刷新后决定，不静默覆盖人工动作。重复提交同一目标且无内容变化可幂等成功。workpad 正文是 GitHub 评论，不复制进本地状态源。

### 3.4 接口与配置

新增 `tracker.kind: board` 明确表示本地 tracker，GitHub client 仅承担来源读取和 GitHub API。保留通用 tracker 读取接口；原生 `github` tracker 与本地 `board` 状态必须分开，不能把 GitHub 的 open/closed 校验偷换为本地状态。

```yaml
tracker:
  kind: board
  provider:
    repo: owner/repo
    token: $GITHUB_TOKEN
    state_dir: /path/to/task-state
  required_labels: []
  active_states: [Todo, In Progress, Merging, Rework]
  terminal_states: [Closed, Cancelled, Canceled, Duplicate, Done]
```

- `fetch_issues_by_states` 和 `fetch_issues_by_ids` 返回归一化任务：`Issue.state` 为本地状态，`native_ref` 保留 repo、GitHub issue number 及原生状态；候选过滤、运行中刷新、continuation 和重启用同一来源。
- `GET /api/v1/tasks`、`GET /api/v1/tasks/:id`：查询本地任务快照。
- `PATCH /api/v1/tasks/:id`：字段更新，包含 expected_revision；输入 state/branch_name/PR/workpad 指针，非法枚举 400、无权 401/403、冲突 409、存储失败 5xx。
- `task_api`：面向 agent 暴露上述读写能力，**agent写和看板写同样强制expected_revision**，宿主持有凭据；不建立第二套 deliver/approve/park 协议。
- `github_api`：继续由宿主持GitHub token，worker环境仍剥离token。移除旧交付/标签授权规则；本期明确扩展现有configured-repo请求通道以支持下列流程端点，不以host业务动作代替：GET查询issue/PR/三类评论/reviews/commit checks/statuses/CI结果；POST创建issue/PR/评论/行内回复/issue或PR的labels及仓库标签；PATCH更新issue的state与评论body、PR的title/body/state；PUT `/repos/:repo/pulls/:n/merge`（squash，携带最终核对head SHA）；DELETE `/repos/:repo/issues/comments/:id`（workpad删除）。创建`symphony`标签缺失时先查后建，权限不足写blocker brief，不跳过完成门槛。保留认证、方法/相对路径及配置repo边界校验；上述端点有mock工具回归。git clone/fetch/push继续复用现有SSH认证，不注入GitHub token。
- 官方land的`gh`查询/写入命令在本地skill中逐项换成上述`github_api`调用，使用agent会话内轮询；不运行依赖裸gh凭据的`land_watch.py`，不构造token透传脚本。文档注明这项认证载体替换；冲突处理、检查等待、反馈处理、squash合并语义保留。检查日志不能取得时报告具体权限缺口，不能宣称已修复。
- 看板沿用现有同源 `/api/task` 代理作为 UI 入口可以，但仅代理上述同一更新契约，不再独立解释 approve/rework 等操作。人工评论经调度服务受鉴权的代理请求转发到github_api，board不持GitHub token；仅允许评论已关联的来源issue。

### 3.5 看板行为

主列：Backlog、Todo、In Progress、Human Review、Merging、Rework、Done。Cancelled/Closed/Duplicate 在终止筛选中可见。列归属只读 task.state；运行、重试和 blocked 是徽标，不决定列。

详情：GitHub 来源标题/描述、任务状态、来源链接、关联 PR、workpad 入口/内容、现有执行日志。状态选择器或等价按钮修改同一个 state 字段；“批准并进入合并”只做 Human Review → Merging，“返工”只做 → Rework。

人工反馈直接在 GitHub issue 评论记录；看板可复用已有评论输入框转发普通评论。反馈正文与状态更新不是跨系统事务：先确认评论成功，再修改状态；第二步失败时提示只需重试状态，避免丢反馈或重复评论。无 `[board]` digest 特殊协议。空白任务、错误态、操作冲突和请求失败必须有反馈。

## 4. Agent 工作规程

以固定版本 WORKFLOW 的状态路由、执行顺序和质量要求为源，仅替换 Linear 工具/链接与项目特定命令。

1. 开始按 ID 重读任务、判断状态；Todo 先转 In Progress，再找/建 workpad，然后开始分析和改代码。
2. 单一 `## Codex Workpad` 评论，包含 Plan / Acceptance Criteria / Validation / Notes / Confusions 和环境 stamp，持续原地更新。兼容已有`## Symphony Workpad`：优先沿用记录中的comment_id或识别旧标记，更新时统一标题，不额外创建第二条；真正Rework仍删除重建。按 GitHub 的可用字段处理其没有的 resolved-comment 属性，不编造该属性。
3. 先复现、制定验证方案、同步 main；票据自带测试要求不得降级；越界改进另建 Backlog 任务。
4. 交 Human Review 前扫描 PR 顶层评论、行内评论、review 汇总；逐项修复或有理由回复，重新验证与 push；确认最新 checks 通过且 PR 已关联、workpad 完整。
5. Human Review 停工。人确认后进入 Merging；worker 按 land skill 的完整流程合并，再置 Done。
6. Rework 按官方 Step 4 完整重做；分支关联 PR 已 closed/merged 时也不得直接复用旧分支。

移植 land 时同时核对 `land_watch.py`、commit/push/pull 和运行验证等依赖。保留检查、反馈、冲突和 head 更新处理；官方项目特有的 review comment 前缀、媒体上传工具和 CI 约定必须绑定当前项目真实配置，不能在本项目不存在对应服务时无条件等待该事件。缺少可替换工具时明确报告能力缺口，不能写成已支持。此为环境适配，不允许借机引入 host 合并或减少审核步骤。

## 5. 调度和恢复

- Orchestrator 保持通用：active/terminal、dispatchable、required_labels（本地默认空）、claimed/running、并发。
- 正常退出后约 1 秒 continuation：刷新真实任务；仍 active 则继续，Human Review/Backlog 则释放，terminal 则清理。该 1 秒重查本身不是 bug，不能改成 merge_poll。
- 异常指数退避、stall 检测、线程内多 turn、workspace 恢复均沿用官方规则。
- In Progress/Merging/Rework 进程重启后仍是 active，可以重新派发；不先退回 Todo、不凭业务状态认为已有 worker 永久占有。
- API 更新可唤醒已有调度 refresh，但不增加另一个定时工作流控制器。

## 6. 现有实现收敛与迁移

当前代码已经实现 v1 部分行为，但 v1 测试通过不能证明 v2 已完成。重点差异：

| 当前实现 | 本次目标 |
|---|---|
| 五态、`review` | 官方七主态及终止别名 |
| adapter 只接受 GitHub open/closed | 独立 board adapter 输出本地状态 |
| routable 仅 todo | 通用 active_states |
| admission 改 In Progress，拒绝 in_progress | agent 更新状态；恢复/续跑支持全部 active |
| HostController 后台合并 | 删除启动与自动处理路径；worker land |
| approved_sha / delivery / repair_budget / merge_eval | 不再授权或调度；迁移时保留原始备份 |
| 评论 digest / `[board]` 触发 | 普通反馈 + 人工改状态 |
| github_workflow(status/deliver/pause) | task_api + 通用 github_api |
| 五列 UI / runtime 映射业务列 | 七主态 UI，task.state 决定列 |

迁移：停服务后备份整个任务目录，显式一次性迁移并保存 schema_version。v1 backlog/todo/in_progress/review/done 对应 Backlog/Todo/In Progress/Human Review/Done；仅有旧stage时，明确merged事实才能迁Done，closed但无merged事实迁Closed，不因PR关闭未合并判Done。没有schema_version的记录按旧格式处理；只处理任务`<number>.json`，忽略*.bak/*.tmp。

- `delivery.pr/sha`、已有PR URL/branch、workpad_comment_id显式迁入新关联字段（sha作为pr_head_sha参考，不是批准令牌），Human Review详情必须保留原PR入口。
- 旧`pending_merge:true`但没有已确认merged事实：迁Human Review，迁移报告逐条标明“合并请求已发出、结果待人工核对”；不自动重发merge、不自动丢弃关联。
- 旧approved_sha/merge-approved不自动迁Merging；全部待审交付先留Human Review。blocked任务迁Human Review，并保留原因。
- 原始旧字段完整保存在离线备份/迁移报告引用中；新运行记录只保留新schema字段和必要关联，不再读attempts/cycle/feedback_claim/repair_budget/host.stage作为策略输入。
- 旧标签不持续驱动调度、不主动删除。损坏、仓库不符或含歧义的记录逐条报告，不覆盖、不开启自动执行。

回滚使用完整备份和旧配置/版本，不让旧代码反向解释新 schema。只操作本地样本验证迁移，不在本轮迁移生产数据。

## 7. 验收标准

v2 重新定义 AC；旧任务表中的同名 AC 及旧通过数量不能沿用。

| ID | 结果 | 验证与通过标准 |
|---|---|---|
| AC01 | 官方状态模型一致 | board adapter 输出本地状态；七主态可见；active/terminal 与示例一致，非活跃不派发 |
| AC02 | 主链路成立 | 受控工具/集成测试证明 Todo 首先改 In Progress，交 Human Review 后释放；人工改 Merging 才重新派 worker |
| AC03 | worker 合并 | 工具mock证明无worker token时可PUT merge、PATCH关PR、DELETE workpad、维护symphony标签；Merging走land、合并前重读状态，CI失败/冲突可处理，成功才Done；无host自动merge |
| AC04 | 反馈与返工遵循原文 | 覆盖三类 PR 反馈；Todo 带 PR 增量修复；Rework 关闭旧 PR、新分支、新 workpad；新评论本身不自动派发 |
| AC05 | 人工等待可恢复 | blocker brief 可查看；Human Review 停工；人工改回 active 后恢复；input_required 按已有运行时语义可见 |
| AC06 | 续跑与重启一致 | In Progress/Merging/Rework 均可恢复；正常退出重查、错误退避、terminal 清理；无五态残留门控 |
| AC07 | 单一写入与看板一致 | 两个并发/过期更新不会覆盖状态；UI按task.state分列；成功、失败、409及反馈部分成功均显示真实结果 |
| AC08 | 迁移安全 | 样本覆盖v1五态/blocked/旧批准/pending_merge/损坏/仓库不符；PR/workpad关联保留；忽略bak/tmp，备份可恢复；旧批准不自行转Merging |
| AC09 | 工程回归及交接 | Python pytest、board pytest、node刷新测试通过；固定版本prompt/skill依赖可用；记录环境验收边界，无真实PR写操作 |

## 8. 边界与确认

- 阻塞业务确认项：无。用户已选择原样流程和指定实施子代理。
- 本期取舍：Merging 等 CI 会占 worker 槽，Human Review 没有后台自动返修监听，均接受官方基线。
- 待环境验收：真实 GitHub 认证、PR评论/CI/合并、199运行环境；本轮不自动执行这些外部写入。
- 保留现有 Python/pi 技术栈；不引入 Linear SaaS、GraphQL仿制服务、任意新依赖或多仓库产品化。
- 日志增强、时间线、归档、报表、自动批准、无人值守评论返工等全部后续单独设计。
