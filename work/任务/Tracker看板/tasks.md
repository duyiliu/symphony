# Tracker 看板 - 执行队列

> 状态：done（真实端到端验收 V301 已通过；三个实测缺陷已修复并复验；199部署/生产迁移仍未执行）
> 当前设计基线：./design.md（v2，已确认；independent设计审查PASS，2/3轮）
> v1 设计基线：五态 + host 合并，已被用户撤销，不作为实施依据
> 当前子代理：23c6fa41-b5da-4988-b8c7-fa12c095314b（serverId=srv_TL10DcJGMtb1；Devin SWE-2 / swe-2-high / thinking=max / smart）
> 并发约束：实施由指定Devin子代理完成，当前已交主代理；主代理独立核验完成，停止业务代码修改；禁止继续委派
> 授权依据：用户“原样照搬先不要扩展”，并明确“先出方案”“devin swe2 max 来实施，用子代理”
> v2 验证：主代理最终复跑Python161、board33、Node9全部通过；受控浏览器复验旧revision冲突/重决/评论不重发/描述与阻塞提示/成功后卡片与抽屉一致全部通过
> 代码基线 branch：main
> 代码基线 commit：e5acc057d8fdd95c6a402ccf9f0e6a203040a564
> v2开始前已有修改：tracked共16文件（git diff --stat为1206 insertions/989 deletions），另含未跟踪host.py/rework.py/task_record.py及测试、work资料；不可当作干净HEAD或批量reset；此前所有修改均保留为实施输入
> 任务前既有工作区修改：v2 评论返工实现与看板五列对齐的全部未提交改动（python/symphony/**、board/server.py、board/tests/**、deploy/WORKFLOW.199.md、python/tests/** 等 36 项，见 git status）
> 实现审查：PASS（第二轮增量核验，Critical=0、High=0）
> 实现审查模式：independent（主代理审查指定子代理的实现）
> 实现审查轮次：2/3
> Reviewer：主代理（与Builder独立）
> 环境验收：本地受控浏览器已通过；真实GitHub PR/CI/review、199部署、生产迁移未执行
> 最后核验：2026-11-12T00:37:39Z；53文件aggregate_sha256=88cef6191ed8ca25377e1ab1dfcbd8a84bf0dbc73e8218df8e8571f8a2fbb531；清单见D:/workspace/playground/linear-review/final-manifest.json

## 完成闸门

- [x] 所有 AC 被 Task / 最终核验覆盖
- [x] 所有本地实现 / 返工任务完成
- [x] 关键确定性验证通过
- [x] 对抗式实现审查 PASS
- [x] Critical / High finding = 0
- [x] 待环境验收项列明

## V301 真实端到端验收（用户追加授权）

- 状态：done（2026-11-12）
- 授权依据：用户“帮我真实的测试一轮”；允许为验证创建隔离测试任务、PR、工作流评论并在测试仓库完成合并；不部署199、不触碰真实业务仓库。
- 目标：真实GitHub + 当前源码服务 + 真Pi模型 + 真浏览器贯通导入/派发/Human Review/Rework/Merging/Done。
- 环境：独立配置与数据在`.tmp/linear-live-20261112/`；API127.0.0.1:18320、board127.0.0.1:18322，由agent-app-manager管理，原服务未动。
- 仓库：现有私有duyiliu/symphony-e2e（用途说明为Symphony E2E测试，启动前无open issue/PR），任务https://github.com/duyiliu/symphony-e2e/issues/1。
- 实际模型：new-api/deepseek-v4.1-flash，真实调用返回LIVE_MODEL_OK；Windows本机SSH密钥不可用，测试配置用现有gh keyring的HTTPS credential helper；token仅服务启动内存中读取，worker仍剥离环境token。
- 已验证：服务与board已就绪、端口归属受管PID匹配，真实任务自动导入Backlog，真浏览器space=2显示任务。
- 后续：无（本阶段完成）；残余小顺延项见下方“顺延项”。
- 进度与发现：
  - 首次Todo派发即暴露真实缺陷P1：adapter把created_at/updated_at原始str传入Issue，runner渲染提示词即AttributeError('str' object has no attribute 'tzinfo')；先前mock验证未覆盖。已修（parse_datetime归一化），新增穿透store→adapter→to_template_map回归。
  - P2：真实GET tasks/1/source耗时19.3s（串行GitHub代理），board 15s超时先502且抽屉静默；已修（source超时45s/comment 30s + 来源失败显式提示），非静默。
  - P3：PATCH已持久化但先同步request_refresh（最多阻塞15s），board几乎同时超时→已写成功显示“操作结果待核对”；已修（先返回200再best-effort刷新，_json补flush；import同根因同修），新增真HTTP阻塞回归。修复后实测：Rework/Merging操作均返回明确成功toast与revision。
  - 全链路真实验证通过：issue#1自动导入Backlog(rev1)→看板转Todo→worker真实执行（真模型、真git、真PR#2、真Actions CI success）→停驻Human Review(rev8)未自批→看板评论+转Rework(rev9)→旧PR#2关闭未合并、旧workpad删除、新分支gh-1-rework-1、新PR#3 CI success、新workpad→Human Review(rev12)→看板评论+确认对话框后转Merging(rev13)→worker按land流程复核check-runs/reviews/comments、squash-merge PR#3（merge commit 24e2319）→Done(rev14)→GitHub自动关闭issue#1（COMPLETED）→看板卡片在Done列、抽屉显示源已关闭+PR已合并。
  - 顺延项（非阻塞，未修）：task_api清除pr_number用0会被拒（须用null）且schema/文档未说明清空方式，workpad_comment_id却又接受0，校验不一致；worker曾中transient pr_number=2+pr_url=""残留后自愈；终态workspace GH-1未即时删除（清理在编排器启动时执行）；看板头部仓库chip计数可短暂stale（既有已知）。
- 结果证据：`.tmp/linear-live-20261112/`下 first-review.json / rework-start.json / second-review.json / final.json（任务、调度、issue、PR、check-rollup快照）、browser-first-review.json / browser-rework-submit.json / browser-second-review.json / browser-merging-submit.json / browser-final.json（真浏览器观测）、first-dispatch-failure.json（P1实证）、lifecycle.jsonl（状态轨迹）；回归：pytest 163→164 passed、board 34、node 9/9。
- 收尾：测试实例 symphony-live-api/symphony-live-board 已停止（agent-app-manager stop确认）；浏览器页面已关闭；symphony仓库HEAD仍在 e5acc05、未产生任何commit/push；GitHub侧仅测试仓库 duyiliu/symphony-e2e 内的issue/PR/分支/评论/合并。
  - P1（实证阻塞，已修复）：首次Todo派发runner崩溃 `AttributeError: 'str' object has no attribute 'tzinfo'`。根因：`_to_issue` 把记录的 `created_at`/`updated_at` 原始 str 传给 `Issue`，`to_template_map`→`util.iso8601` 期望 datetime。修复：adapter 用 `util.parse_datetime` 归一化（无效/无时区→None，官方口径）。回归：`test_record_timestamps_normalize_for_template_map`（store→adapter→to_template_map 全链）、`test_record_bad_timestamps_degrade_to_none`。证据 `.tmp/linear-live-20261112/first-dispatch-failure.json`。
  - P2（实证，已修复）：真实 `GET /tasks/1/source` 实测 19.31s 超 board `fetch_task_source` timeout=15 → 502 → 抽屉静默空。修复：source 超时 45s（调度侧串行 issue+评论页+PR 查询）、comment 30s；抽屉对来源失败显式显示"来源加载失败：原因，可重开抽屉重试"（不再静默当无描述）。
  - P3（实证，已修复）：PATCH 本地写入已成功后 handler 先调用 `orchestrator.request_refresh()`（慢 GitHub poll 最长 15s）再 `_json(200)` → board `request_task_patch` timeout=15 先超时 → 已写成功被报"操作结果待核对"。修复：`_handle_task_patch`/`_handle_task_import` 改为先 `_json(200, record)` 再 best-effort `request_refresh`；`_json` 增加 `wfile.flush()`（BufferedWriter 不保证即时送达，后续阻塞不能扣住响应字节）。回归：真实 HTTP 用例 `test_patch_response_not_blocked_by_refresh`——`request_refresh` 用 `threading.Event` 阻塞，客户端仍及时收到 200+revision=2，`finally` 释放 Event 并 `shutdown/server_close`；旧顺序下该用例会确定性命中 urlopen 10s 超时。
  - 回归：`pytest tests/` 164 passed、`pytest board/tests/` 34 passed、`node --test` 9/9、嵌入 JS node --check 通过。真实端到端复测由主代理重启隔离实例后执行。

## V302 214 环境真实端到端验收（用户追加授权）

- 状态：done（2026-11-12）
- 目标：在 214（CentOS 7）独立部署同一套代码，跑一轮完整真实生命周期，验证跨主机可移植性。
- 环境：`/root/symphony-e2e` 独立目录；API 0.0.0.0:18320、board 0.0.0.0:18322；systemd `symphony-e2e-api` / `symphony-e2e-board`；不影响 199 与既有服务。
- 运行时栈：214 无 python3/pi，故安装 python-build-standalone 3.12.16（免编译，scp 传入）、PyYAML 6.0.3（阿里云镜像）、node 22.22.0 **glibc-217 非官方构建**（官方二进制需 glibc≥2.28，CentOS7 不可用）、pi 1.5.2（npm 本地安装）；pi 配置隔离在 `PI_CODING_AGENT_DIR=/root/symphony-e2e/pi-agent`，模型 new-api/deepseek-v4.1-flash 真实返回 LIVE_214_MODEL_OK。
- 凭据：214 本机已有 gh 登录（duyiliu，repo/workflow scope），服务启动时 `gh auth token` 注入内存；worker 仍剥 env token；未传输任何长期 token 文件。
- 实测障碍与处置：214 可直连 api.github.com，但 **github.com:443 的 git 通道不通**（clone 报 Encountered end of file）；改用 199 HTTP 代理 `http://192.168.1.199:7890` 写入 hooks/全局 git config 后 clone/push 正常。首次派发失败已实证记录并在修后重派。
- 链路结果（issue #4）：导入 Backlog(rev1) → 看板 Todo(rev2) → 首次派发因 git 网络失败重试（已暂停回 Backlog）→ 修复后 Todo(rev4) → worker 真执行 In Progress(rev5) → PR #5 双 check SUCCESS + symphony 标签 + workpad → Human Review(rev7) 停驻不自批 → 看板评论+转 Rework(rev8，响应 3.6s) → 旧 PR #5 CLOSED 未合并、旧 workpad 清空、新分支 gh-4-rework-1、新 PR #6 双绿、新 workpad → Human Review(rev12) → 评论+确认弹窗 → Merging(rev13) → squash-merge PR #6（7e9678f）→ Done(rev14) → issue #4 自动 CLOSED(COMPLETED) → 看板 Done 列、抽屉显示源已关闭+PR 已合并。
- 额外观测：远程（Windows）直连 214 的 `/api/v1/*` 返回 403，符合 R201 的 loopback/Bearer 访问控制；看板本身可正常远程访问。终态 workspace 已清理。
- 证据：214 上 `/root/symphony-e2e/evidence-final.json`、`evidence-lifecycle.jsonl`、`sessions/`（3 个真会话）、`sessions-live/`；Windows 侧 `.tmp/linear-live-20261112/browser-214-final.json`、`rework-214.md`、`issue-214.md`。
- 收尾：`symphony-e2e-api` / `symphony-e2e-board` 已停止并 **disable（不再开机自启）**，备份 cron 已移除，18320/18322 已释放；浏览器 space 3 页面已关闭；symphony 仓库无本次未提交改动。部署物料与证据（`/root/symphony-e2e`，480MB）保留在 214 便于随时复测，不自动运行。
- 定位说明：214 实例为**临时测试用途**，验收完成后已关闭；若要正式生产，重新 `systemctl enable --now symphony-e2e-api symphony-e2e-board` 并恢复备份 cron 即可（见 `deploy/e2e-214/README.md`）。

## v2 验收覆盖

| AC | 任务 | 当前证据 |
|---|---|---|
| AC01 官方状态模型 | V201、V205 | 已实现+本地验证：store.STATE_NAMES 七主态+四终止别名；board COLUMNS 七列+STATE_TO_LANE 映射；test_tasks/test_server 断言覆盖；真实环境联跑待验收 |
| AC02 主链路 | V202、V204 | 已实现+本地验证：tracker 门面按 kind 分派；active_states=Todo/In Progress/Merging/Rework；deploy WORKFLOW×2 移植官方Step0–4+workpad模板+完成门槛；mock测试147过；真实AI端到端待验收 |
| AC03 worker合并 | V203、V204 | 已实现+本地验证：allowlist放行PUT merge(squash+head sha)/PATCH pulls state/DELETE comments/POST labels；land SKILL=github_api+会话内轮询+merge前task重读；worker env无token；真实PR合并未实测 |
| AC04 反馈及完整返工 | V203、V204 | 已实现+本地验证：prompt规定Rework关旧PR/新分支/新workpad+原样重做；reviews/inline comments端点放行；in_reply_to数值约束在land；真实评审流待验收 |
| AC05 人工协作及解阻 | V202、V204、V205 | 已实现+本地验证：评论先写后改态、展示revision提交、冲突后重读重决、源关闭与阻塞提示；主代理受控真实浏览器复测通过；真实PR协作待环境验收 |
| AC06 续跑与重启 | V202 | 已实现+本地验证：orchestrator test覆盖running占容量/DONE_NORMAL续跑/claim-skip释放/退避等容量/terminal清理；runner消失或非active即结束 |
| AC07 单一写入/UI一致 | V201、V205 | 已实现+本地验证：PATCH revision保护、任务读API鉴权、board无GitHub凭据；Node9及受控浏览器覆盖旧revision拒绝/刷新后重决/评论只发一次/成功后列与抽屉一致 |
| AC08 安全迁移 | V201 | 已实现+本地验证：migrate.py独立读v1(host.stage/delivery/attempts/cycle回退)、备份先于写入、pending_merge→Human Review+待核对报告、bak/tmp跳过、损坏逐条上报；test_tasks迁移用例过 |
| AC09 回归和交接 | V206 | 主代理复跑Python161+board33+Node9全绿，独立实现审查2/3轮PASS；受控浏览器通过，真实GitHub/199/生产迁移待验项已列 |

## v2 执行队列

### V201 本地tracker与可恢复迁移

- 状态：done（本地实施、验证与主代理核验完成）
- 依赖：无
- 目标：独立board tracker，JSON记录唯一权威，GitHub发现与来源内容缓存，显式迁移。
- 对应验收：AC01、AC07、AC08
- 验收标准：七主态及终止别名，按本地状态读取；open发现失败不抹掉已有记录；closed来源仍可见；repo隔离、revision冲突保护；迁移保留PR/workpad和pending_merge待核对信息。
- 验证方式：有意义的store/adapter/迁移及并发回归测试；只能操作临时样本目录。
- 验证证据：
  - 新模块 python/symphony/tasks/{store,adapter,importer,migrate,tools}.py + python/symphony/tracker.py 门面（kind=github/board 分派）
  - store.py：SCHEMA_VERSION=2、STATE_NAMES 7主态+4终止别名、per-issue锁+原子写、revision单调递增、HISTORY_LIMIT=50、repo隔离校验、refresh_source只刷缓存不碰业务态
  - importer.py：open分页发现→缺记录建Backlog（已有记录不动业务态）；closed仅按编号显式导入→Closed；404省略/错误不伪造
  - migrate.py：独立读v1（record.host.stage + delivery.published/attempts/cycle回退链，复刻task_record.state_of优先级）；备份目录先于写入（修复了先写后备份缺陷）；pending_merge→Human Review+待人工核对报告；<number>.json限定，bak/tmp跳过；损坏/串仓/歧义态逐条上报不静默
  - 测试：python/tests/test_tasks.py（56用例）+ test_github_record.py 记录层覆盖
  - 真实缺陷修复记录：TaskError继承WorkflowError入既有错误路径；$ENV先解析再校验路径；upsert_source嵌套取锁死锁修正

### V202 调度与续跑恢复回归官方通用规则

- 状态：done（本地实施、验证与主代理核验完成）
- 依赖：V201
- 目标：Todo/In Progress/Merging/Rework均由active_states决定派发，agent改状态，移除host所有权和merge_eval旁路。
- 对应验收：AC02、AC05、AC06
- 验收标准：Human Review停工；1s continuation刷新真实任务；重启恢复active任务、异常退避、terminal清理；内存blocked保持原运行时语义。
- 验证方式：runner/orchestrator单测覆盖active状态、停工、恢复和终止路径。
- 验证证据：
  - runner.py/orchestrator.py 全部改经 tracker 门面；merge_eval/admission/host钩子路径移除；Human Review不在active_states不派发
  - orchestrator._maybe_dispatch() 先 import_sources 再取候选；发现失败不抹已有记录
  - board adapter fetch_candidate_issues 扫本地记录按本地state；fetch_issues_by_ids显式刷新做live 404检查，closed不覆盖本地态
  - 测试改写：test_orchestrator_units.py（容量/DONE_NORMAL续跑/claim-skip释放/退避等容量/terminal清理）+ test_github_tasks.py（本地态驱动派发）
  - runner：刷新任务消失或非active即结束；active进入下一turn

### V203 任务API与GitHub工具贯通

- 状态：done（本地实施、验证与主代理核验完成）
- 依赖：V201
- 目标：同一鉴权服务承载task_api、看板代理和github_api；移除旧交付协议限制，点名放行设计所需端点。
- 对应验收：AC03、AC04、AC07
- 验收标准：agent与UI同样expected_revision；merge/PR关闭/workpad增删改/标签调用可用；worker无raw token；错误与409可见；board不持GitHub token。
- 验证方式：HTTP/工具mock测试；不得发送真实评论、创建issue、改标签或merge。
- 验证证据：
  - http_api.py：/api/v1/tasks 列表/单查/source/comments/PATCH/import 全路由；PATCH强制expected_revision（revision_conflict→409）；鉴权=loopback或配置Bearer；旧/api/v1/github/task与github_workflows投影移除
  - /api/v1/tasks/<id>/source：宿主持token代理读issue+workpad(兼容Codex/Symphony标记)+PR徽标，board零token
  - agent_tool.py：github_api仅相对路径（含://或非/开头→invalid_arguments）；task_api对board kind暴露；tool描述注明替代gh
  - workflow_policy.allow_request：GET三评论/reviews/checks/statuses；POST issues/PR评论/inline回复/labels；PATCH issue state与评论body、PR title/body/state；PUT pulls/:n/merge；DELETE issues/comments/:id
  - mcp_bridge经tracker门面转发tool_specs/execute_agent_tool
  - 测试：test_github.py重写（policy放行+相对路径拒绝完整URL+sanitize）

### V204 移植官方prompt和land等执行规程

- 状态：done（本地规程移植与主代理核验完成；真实PR环境未测）
- 依赖：V202、V203
- 目标：本地tracker工具替换Linear工具，移植workpad、feedback sweep、阻塞简报、Rework完整重做和worker land。
- 对应验收：AC02、AC03、AC04、AC05
- 验收标准：Todo先改态后工作；Human Review前检查与反馈满足门槛；Merging每轮和merge前重读状态；Rework关闭旧PR/新分支/新workpad；git走已有SSH，land工具轮询不用裸gh凭据；所有引用技能和本项目命令可解析。
- 验证方式：模板渲染/依赖核验及受控工具序列测试，不用脆弱的全文字符串镜像测试替代行为验证；真实AI远端业务验收待授权。
- 验证证据：
  - deploy/WORKFLOW.md 与 deploy/WORKFLOW.199.md 完全一致（diff为空，各265行）：官方Step0–4、唯一持久`## Codex Workpad`（环境stamp+Plan/Acceptance Criteria/Validation/Notes/Confusions）、票据验证项拷贝进workpad、完成门槛=workpad✓+AC✓+最新PR checks绿+feedback sweep+分支已推+PR已链+symphony标签+适用runtime验证、escape hatch、blocker写workpad Notes（无workpad才建一条blocker评论）、不改issue body跟进、超范围另开issue、Human Review→Merging仅人工、revision_conflict→重读重决
  - github_api全部示例为相对路径；git用SSH（git@github.com:duyiliu/scenario-test.git）；默认分支=master（实测default_branch:"master"），分支基于origin/master，PR base=master
  - deploy/skills/land/SKILL.md：官方land移植版，gh→github_api、git fetch/merge/push走SSH、sleep会话内轮询、每轮等待及merge前task_api get重读非Merging即停、PUT merge squash+verified head sha、Done仅在GitHub确认合并后写、禁auto-merge、symphony标签前置、评审反馈按in_reply_to数值回复、冲突用`git fetch origin master && git merge origin/master`
  - 真实PR合并/CI轮询/评审流未实测（受控工具序列已由policy单测覆盖端点约束）

### V205 看板按任务状态展示与操作

- 状态：done（本地实施、回归与主代理受控浏览器核验完成）
- 依赖：V201、V203
- 目标：七主列、终止状态筛选、同一state写入、来源关闭提示、workpad/PR/既有日志展示。
- 对应验收：AC01、AC05、AC07
- 验收标准：runtime徽标不改变业务列；改态和409/失败反馈真实；评论先成功后改态，部分成功可重试；UI说明在途GitHub请求不可撤销。
- 验证方式：board pytest、node刷新测试及受控本地浏览器流程；用浏览器前读取ego-browser技能。不得用真实任务数据写操作验收。
- 验证证据：
  - board/server.py重写（3950→2766行）：七列COLUMNS+STATE_TO_LANE（终态别名并入Done列保留原态文本）；卡片带data-rev/data-task-state/data-source-state/PR链接
  - 写路径=/api/task单口：op=move（评论先写→PATCH expected_revision）/op=comment；revision_conflict→409带comment_posted标志（前端提示"评论已发布请重新决定"不重发评论）；loopback调度器转发+可选SYMPHONY_API_TOKEN Bearer
  - board零GitHub token零直连：数据经/api/v1/tasks+source代理（新增/api/source/GH-N路由转发）
  - runtime徽标（running/retrying/blocked）只改tone不改lane；源关闭=徽标文案追加不覆盖本地态
  - 抽屉：任务事实（状态/修订/源态/分支/workpad）+代理source（issue/workpad/PR）+移动按钮（全态）+评论框+会话记录链/实时视图/运行记录
  - 已完成视图改由本地终态记录驱动（merged_pr来自record.pr_url）
  - write_guard前置锁外（跨站拒绝不占串行锁）；抽屉竞态OPEN_SEQ防护；提交网络异常捕获
  - 真实缺陷修复记录：applyFilter隐藏逻辑取反（空查询曾藏全部卡片）；BOARD替换边界修复；rep参数修正
  - 测试：board pytest 32过（七列/别名映射/源关闭徽标/rev携带/评论顺序/409标志/跨站/并发锁/completed）+ node 6过（列集合/applyFilter空命中不命中/pending确认/revision提交/conflict提示/异常不谎称成功）；受控浏览器流程未做（无真实任务数据）

### V206 集成验证与交付核验

- 状态：done（本地核验与独立实现审查PASS；环境验收边界已列）
- 依赖：V201–V205
- 目标：清点旧钩子/host启动/旧协议残留，所有AC与新证据对应，交主代理核验。
- 对应验收：AC01–AC09
- 验收标准：Python pytest、board pytest、node测试通过；部署配置仅本地修改；无host自动合并/旧批准权威/专用merge_eval；设计说明与代码一致；未实测远端能力明确列出。
- 验证方式：必要静态检查、全量回归、差异审查；记录命令、退出码、时间和代码标识。
- 验证证据：
  - 全量回归（真实退出码）：`cd python && python -m pytest tests/ -q` → 147 passed；`python -m pytest board/tests/ -q` → 32 passed；`node --test board/tests/test_refresh.mjs` → 6/6 pass
  - 残留清点：python/symphony 及 board/server.py 中 merge_eval/issue_blocked/same_phase/prepare_rework/github_workflows/pause_reason/request_task_operation/load_tracker/is_merge_evaluation 均无运行时引用（仅migrate docstring说明性提及state_of）
  - 五文件废弃依据：github/{workflow,workflow_api,host,rework,task_record}.py 互相引用构成孤岛，无任何外部导入；外部备份 C:/Users/ZHANGQ~1/AppData/Local/Temp/symphony-linear-legacy-nxl000l5（含manifest哈希）可恢复；删除决定留主代理
  - v1旧测试 .py 已移除（仅__pycache__残余），有效覆盖已迁入v2：test_tasks(56)+test_github(重写)+test_github_record+test_github_tasks+test_orchestrator_units+test_board_session_status+board tests
  - cli.py 无HostController启动；http_api无github_workflows投影；board无GitHub token路径
  - client.py docstring修正；deploy两WORKFLOW文件diff为空
  - 备份：board/server.py 改前副本 D:/workspace/playground/board-v205/server.py.bak（字节级校验一致）
  - 时序缺陷修复：board路由write_guard前置锁外（跨站拒绝不再占_task_lock）；测试drain改有界自旋+并发用例改直连route_post消抖（4连跑32/32稳定）

## v2 实现审查返工（第一轮集中报告）

主代理复跑Python147、board32、Node6全部通过；以下为额外验证发现的实际问题，修复不能只改测试断言。证据脚本与输出：D:/workspace/playground/linear-review/build_fixture.py、evidence.json、fixture.html、ui-evidence.json。所有请求仅mock或本地fixture，无真实GitHub写入。

### R201 来源读取鉴权缺口

- 状态：done（主代理第二轮复核通过）
- 严重度：High
- 对应：AC07、AC09，design §3.4受鉴权宿主代理
- 场景与证据：http_api.do_GET的tasks及source路径未调用鉴权；以203.0.113.10客户端、无Authorization构造handler，_mutation_allowed=False，但GET /api/v1/tasks/4/source仍返回200和PRIVATE_BODY_SENTINEL，并触发宿主持凭据的来源查询。列表/单查同样无保护。
- 要求：任务列表、单查、source读取均沿用同一loopback/Bearer访问边界；source先确认task存在且repo匹配，不做未建档issue的任意代理。
- 验证：remote无token/错误token拒绝且不调用GitHub；正确API token/loopback成功；未建档/串仓拒绝。
- 修复：`_handle_tasks_get` 入口统一 `_mutation_allowed()`（loopback/Bearer）前置校验；`_handle_task_source` 先 `task_store.get` 确认已建档（404 未建档，TaskError 按错误码映射），再代理 GitHub。
- 证据：`test_task_reads_reject_remote_without_token`（remote 无 token/错 token 三条 tail 均 403）、`test_task_reads_allow_bearer_and_loopback`（Bearer api-secret / loopback 200）、`test_source_proxies_only_registered_tasks`（未建档 404 且不调用 gh_client.request，仅 issue+comments 两调用）、`test_source_rejects_remote_and_corrupt_records`（remote 403、损坏记录 409 非假 404）。

### R202 github_api无法分页

- 状态：done（主代理第二轮复核通过）
- 严重度：High
- 对应：AC03、AC04
- 证据：agent_tool schema没有params；execute固定gh_client.request(..., {}, ...)，policy又拒绝path中的?。评论/reviews/check-runs多于默认首30项时agent不能读取后续页，违背完整feedback sweep；官方工具有params。
- 要求：恢复可选params对象，正确转发page/per_page等查询；保持相对路径和repo权限边界，不把query当路径绕过。
- 验证：通过MCP/tool实际调度到mock client，至少读取第二页反馈；schema/参数类型校验和边界仍有效。
- 修复：schema 新增 `params`（可选对象，值仅标量）；`execute_agent_tool` 校验 `_valid_params` 并透传 `gh_client.request`；`?`/`#` 出现在 path 直接 invalid_arguments（urlsplit 会静默剥离 query 导致绕过 allowlist，已在参数层拒绝）。
- 证据：`test_params_forwarded_for_pagination`（mock client 收到 {"page":2,"per_page":100}）、`test_params_must_be_scalar_dict`（嵌套/数组/字符串/数字四种非法参数均 invalid_arguments 且不调用 request）、`test_query_string_in_path_still_rejected`（query 不再能伪装路径）。

### R203 Store读取缺少身份和结构校验

- 状态：done（主代理第二轮复核通过）
- 严重度：High
- 对应：AC07、AC08
- 证据：Store.get直接_read_file，不校验repo；foreign/repo store.get(4)能读到o/r记录；有效JSON {repo:o/r,state:Todo}落5.json后list不报错，adapter.fetch_candidate_issues抛KeyError:issue_number，整批候选中断。
- 要求：所有读/写路径一致校验repo、schema、任务编号与文件身份、状态和revision等必需结构；格式损坏/旧schema要报告或要求显式迁移，不能默认Backlog、覆盖或让单条阻断正常列表。candidate列表可报告坏项并隔离；ID刷新错误不能假装404。
- 验证：串仓get、错误编号、合法JSON但缺字段/坏state/坏revision均受控失败；正常任务仍可读取和调度；迁移原始样本不被覆盖。
- 修复：新增 `_validate_record`（repo/schema_version=2/issue_number=文件名/identifier/state 合法/revision 正整数）统一应用于 `get`/`locked`/`list`/`upsert_source`；`list` 对坏项报告 errors 并隔离，健康记录不受影响；`import_open_issues` 对单条 TaskError 记 warning 并跳过（不覆盖、不中断）；v1 结构 get 报 `unsupported_schema_version`（409），要求显式迁移；`fetch_issues_by_ids` 中损坏记录抛 TaskError 即刷新失败而非伪装 404。
- 证据：`test_get_rejects_foreign_repo`、`test_get_rejects_number_and_identifier_mismatch`、`test_get_requires_schema_v2_and_valid_state_revision`、`test_list_isolates_corrupt_and_keeps_healthy_candidates`（缺 issue_number 的 5.json 不再 KeyError）、`test_update_rejects_corrupt_record_without_overwrite`、`test_import_isolates_corrupt_record`、`test_corrupt_record_is_fetch_failure_not_phantom_404`。

### R204 UI使用未展示的新revision覆盖人工改动

- 状态：done（主代理第二轮源码、Node与真实浏览器受控fixture复核通过）
- 严重度：High
- 对应：AC07
- 浏览器证据：打开GH-4抽屉显示Human Review/修订1；fixture模拟另一人改Rework/修订2并触发真实refreshIssues；抽屉仍显示Human Review/1，点击Merging却发expected_revision=2。后端会接受这次基于旧视图的批准，revision保护被UI绕过。证据见ui-evidence.json。
- 要求：提交绑定用户实际读取/展示的revision；后台刷新不可悄悄为旧抽屉换提交版本。成功后同步抽屉状态；409及comment_posted后避免保留正文导致重复评论。
- 同一UI范围非阻塞缺项：source接口已有body但renderDrawer完全没展示issue正文，补齐设计中的任务描述；迁移blocked_reason也应可见；终止筛选应能区分Done/Cancelled/Closed/Duplicate，在途合并不可撤销提示需真正出现在UI（当前tasks自报但源码没有）。
- 验证：实际DOM/受控浏览器覆盖后台改态→旧视图提交→409/重新决定；成功后抽屉和卡片一致；评论已发后只重试状态；描述可见。
- 修复：`CUR_REV`/`CUR_SOURCE` 随抽屉打开绑定展示值，`displayedRev` 提交时优先用展示修订（后台刷新不悄悄换版本）；200 后用响应 revision 回填 FACTS 并重绘抽屉；409 时 `await refreshIssues()` 后 `redrawDrawer` 重绑到新展示修订；`POSTED` 去重——409 comment_posted 后同文本不再发送，submitComment 同文本拒绝；drawer 新增任务描述（source.issue.body）、阻塞原因（facts.blocked，build_cards 补充字段）、Merging 持久警告+进入 Merging 的 confirm；completed 面板新增 cstate 下拉区分 Done/Cancelled/Closed/Duplicate。
- 证据：Node `submitMove binds the revision displayed in the open drawer`（FACTS=rev2、抽屉=rev1 → 提交 rev1，正是审查复现场景）、`409 with comment_posted does not repost and rebinds visible revision`（重绑 rev9、重发不带评论）、`submitComment refuses to repost identical published text`、`drawer exposes description/blocked/merge warning`（source 断言）；board pytest `test_card_carries_blocked_reason_for_drawer`、`id='cstate'` 断言。
- 说明：浏览器端复测由主代理 fixture 流程执行；本次仅到 DOM 级 node 断言。

### R205 land误用GraphQL mergeable枚举

- 状态：done（主代理第二轮核对REST规程与工具契约通过，真实PR环境未测）
- 严重度：High
- 对应：AC03
- 证据：工具已改GET /pulls/n（REST），land仍判mergeable == CONFLICTING / UNKNOWN；REST为bool/null，mergeable_state为dirty/unknown等。冲突不会进入修复，405回到同一循环无法收敛。
- 要求：按REST字段识别冲突与暂未计算：mergeable=false且dirty走同步/冲突修复，null/unknown等待重查；保留状态重读、head sha和worker执行者。
- 验证：用实际REST形状样本核对本地land规程与工具调用契约，不用再次引入host业务门禁。
- 修复：`deploy/skills/land/SKILL.md` 步骤4/5与Failure handling改为REST语义——`mergeable` bool/null、`mergeable_state` clean/dirty/unknown 等；`false`+`dirty` 走冲突修复，`null`/`unknown` 等待重查，明确"never the GraphQL CONFLICTING"；保留状态重读/head sha/任务门。

## 返工轮次1 回归证据（Builder 执行）

- `cd python && python -m pytest tests/ -q` → **161 passed in 0.54s**（147→161，含 R201/R203 新用例与 R202 工具用例）
- `python -m pytest board/tests/ -q` → **33 passed in 16.70s**
- `node --test board/tests/test_refresh.mjs` → **9/9 pass**（含 R204 修订绑定/防重发/抽屉展示断言）
- 嵌入 JS `node --check` → 通过；`store` 校验覆盖 get/locked/list/upsert_source 全部读写路径
- 顺手项：adapter `_to_issue` 恢复 `created_at` 传递（官方排序依据，副项不阻塞已闭合）

## 最终独立核验（第二轮）

- 复审范围：仅R201–R205修复和受影响的API、store、工具、land文档、UI行为；未重复首轮完整设计对照。
- 主代理复跑：`cd python && python -m pytest tests/ -q` → 161 passed，exit=0；`python -m pytest board/tests/ -q` → 33 passed，exit=0；`node --test board/tests/test_refresh.mjs` → 9 pass，exit=0；`git diff --check`无错误（仅CRLF提示）。
- R201：GET统一受loopback/Bearer保护，source先验证存在/结构/repo，测试覆盖远端无/错token拒绝和零来源请求。
- R202：params schema和实际client转发已补，分页及拒绝路径query/嵌套参数的回归通过。
- R203：读路径身份/schema/state/revision校验已补，健康记录不受损坏单条影响，原文件不覆盖；迁移仍用离线样本。
- R204受控浏览器：ego space=1，同一page，真实生成HTML/BOARD，所有fetch均fixture mock。GH-4抽屉显示rev1；后台变Rework/rev2后首次提交仍发rev1→409。评论已发仅一次；抽屉刷新为Rework/rev2，人工重决后第二次发rev2且comment为空→Merging/rev3；卡片列和抽屉一致，描述/阻塞原因/在途请求提示可见。证据D:/workspace/playground/linear-review/ui-v2-conflict.json、ui-v2-success.json及round-04至06.mjs；退出码均0，browser TaskSpace已finish关闭。
- R205：land现在识别REST bool/null与dirty/unknown，不再用GraphQL枚举作REST条件；GitHub真实合并未调用。
- 结论：PASS，Critical=0、High=0；本地工程交付完成。真实AI执行/PR合并/CI/review、199部署与生产迁移均留环境验收，不用mock冒充实测。
- 非阻塞观察：legacy五模块已成孤岛、无运行时引用，源码保留且有外部备份；adapter.created_at当前为原始字符串而通用排序器期待datetime，严格按源创建时间排序仍需后续校正；repo静态chip计数仍可能陈旧。这些不影响本期状态流转与人工审核闭环，不继续扩大返工。

## v1 历史验收覆盖（已失效，禁止用作 v2 通过证据）

| AC | 覆盖任务 | 验证状态 |
|---|---|---|
| AC01 单一权威 | T02、T05（grep 断言） | 已验证（无标签写、无 approve API、无 handing_off 写入；add_label/remove_label 已删） |
| AC02 全流程状态机 | T01、T02 | 已验证（24 记录单测 + 生命周期单测） |
| AC03 批准绑定 SHA | T03 | 已验证（approved_sha 门禁 + delivery_changed 测试） |
| AC04 等待态收敛 | T03 | 已验证（issue/PR 三种外部收敛单测） |
| AC05 迁移归一化 | T01 | 已验证（normalize 各 stage/标签→新模型 + .bak） |
| AC06 看板一致 | T04 | 已验证（五列渲染 + 操作按钮 state 驱动单测） |
| AC07 无热循环 | T02 | 已验证（skip→DONE_AWAIT_MERGE 30s merge_poll） |
| AC08 回归 | T05 | 已验证（135 python + 35 board + 19 node 全绿） |

## v1 历史任务队列（仅保留记录，v2 待设计审查后重新拆分）

### T01 任务记录升级为唯一权威 + 迁移归一化

- 状态：done
- 依赖：无
- 对应验收：AC02（部分）、AC05
- 目标：`state_dir/<issue>.json` 收据升级为全量任务记录
- 验收标准：单测覆盖五种状态迁移、blocked 冻结语义、旧记录各 stage→新 state 映射、.bak 生成。
- 验证方式：`cd python && python -m pytest tests/test_github_record.py -x`
- 验证证据：24 测试通过（test_github_record.py）；state_of/normalize/transition + task_operate 六操作 + .bak

### T02 派发/执行链路切换到状态权威

- 状态：done
- 依赖：T01
- 对应验收：AC01、AC02（派发段）、AC07
- 验收标准：单测覆盖 todo→in_progress 原子迁移、已交付任务不产生 1s continuation、merge_poll 30s 一跳。
- 验证方式：`cd python && python -m pytest tests/test_orchestrator_units.py tests/test_github_tasks.py -x`
- 验证证据：135 python 测试全绿；routable=record.todo、begin_run 锁内迁移、skip→DONE_AWAIT_MERGE、标签 admission 全删

### T03 host 合并收敛 + 批准落记录

- 状态：done
- 依赖：T01
- 对应验收：AC03、AC04
- 验收标准：单测覆盖 approve→SHA 变化重需批准、低风险自动合并、三种外部收敛、无悬挂 stage。
- 验证方式：`cd python && python -m pytest tests/test_github_tasks.py -x`
- 验证证据：14 生命周期测试通过；review.approved_sha 门禁、_final_gate review 先行、issue/PR 三种外部收敛、finish_handoff/approve_delivery/approve API 全删

### T04 看板改造：写路径 = 状态迁移

- 状态：done
- 依赖：T01、T03
- 对应验收：AC01（UI 段）、AC06
- 验收标准：五列看板按任务记录 state 渲染；操作按钮按 state 渲染；无标签写路径。
- 验证方式：`cd board/tests && python -m pytest -q && node test_refresh.mjs`
- 验证证据：35 pytest + 19 node 全绿；/api/task 统一写口、record-driven 卡片、laneOf 按 task_state、confirmOperations 按 state/approved 确认

### T05 删除清单核验 + 全量回归 + AC 核验

- 状态：done
- 依赖：T02、T03、T04
- 对应验收：AC01、AC08
- 目标：grep 断言无 `agent`/`needs-human`/`merge-approved` 标签读写、`approve` API、`handing_off`/`delete_grant`/`approval_label` 残留；`deploy/WORKFLOW.199.md` 配置收敛确认；python pytest 全量 + board pytest + node 全绿；逐条核对 AC01–AC08。
- 验收标准：全部确定性验证通过，无残留引用。
- 验证方式：上述全量命令
- 验证证据：grep 无标签写路径；worker merge action 已废（enum 收缩为 status/deliver/pause，_dispatch 拒 merge_host_only）；deploy/WORKFLOW.199.md 改写为记录模型；135 python + 35 board + 19 node 全绿

## 审查记录与返工

- v1 实现审查：未执行；不作为 v2 证据。
- v2 设计首审 1/3：independent，Reviewer=Devin SWE-2 Max（顶部agentId），FAIL。C1=GitHub写通道未明确；H1=land合并前状态复查；H2=来源closed可见性；H3=导入触发与锁；H4=PR关联及pending_merge迁移。
- 主代理已集中修订上述五项；保留官方agent模型，明确在途GitHub请求无法撤销的竞态；未添加host状态门或自动同步控制器。
- 第2/3轮设计复审：同一Reviewer独立复核PASS，Critical=0、High=0，C1/H1–H4均闭合；原生agent并发窗口、轮询成本及Human Review无监听作为明确基线限制。
- 设计阶段结束，正式实施交同一指定子代理；实现审查尚未开始，后续由主代理核对代码与真实验证证据。

## 阻塞项

- 本地交付范围内无阻塞项；第二轮独立实现审查PASS，R201–R205全部闭合。
- 真实GitHub写操作、部署与生产迁移不在本轮范围，保持待环境验收。
- 真实GitHub写操作、部署与生产迁移不在本轮范围。

## 恢复说明

- 优先读取v2 design.md和本文件顶部，从V201–V206推进；不要沿用v1历史完成记录替代v2验证。主代理已正式授权指定子代理实施，后续低成本细节无需反复确认。
- 当前工作区包含大量此前未提交修改，应原地检查并针对性修改，不可 reset / stash / checkout 覆盖已有工作。
- 以下历史任务完成状态和测试数量未经本轮重跑，只作 v1 历史记录。
