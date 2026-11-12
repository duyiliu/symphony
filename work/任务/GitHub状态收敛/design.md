# GitHub 状态收敛（对齐 Linear 语义）

> 设计审查：PASS
> 审查模式：same-context
> 设计审查轮次：1/3
> 设计审查范围与结论：状态流转（skip 热循环、waiting_review 不可达、外部 merged 漏检、handing_off 卡死、stage 合并与读取兼容）、批准层移除（API/标签/看板/收据字段）、merge_eval 保留语义（不占槽重查链）、pause 标签幂等修复、损坏收据 fail closed；未覆盖：rework 评论协议本身（保留）、199 实际运行时验收。
> 设计版本：v1
> 设计确认：待确认
> 设计确认依据：无
> 本文件只保存稳定设计基线；执行进度、实施清单、返工状态统一放同目录 `tasks.md`。

## 1. 背景与范围

### 背景

GitHub 评论返工 v2 引入了执行/交付分离的完整状态机（design 已 PASS 并实施）。复审发现两层问题：

1. 自加的批准层（`approval_label` + `/api/v1/github/approve` + 看板批准按钮）与 SPEC §附录 B"Board actions MUST NOT substitute for a required review approval"及 Elixir 原版语义冲突，且在 199 部署上未配置 `approval_label`，按钮必失败；
2. 状态机新增接缝处存在实际缺陷：`skip` 被当正常完成导致 1 秒热循环、门禁顺序使 `waiting_review` 不可达、requeue/rework 分支先返回导致外部已合并 PR 漏检、`finish_handoff` 对已关闭 Issue 永久卡在 `handing_off`、旧交付迁移漏 `delivery["issue"]`、approval 时间下界过严、pause 标签写失败不重试、损坏收据无限退避重试。

用户决定走"含义二"：GitHub 不变，按 Linear 工作流语义收敛。看板列已对齐（未排期/待领取/进行中/审核中/已完成，blocked 降级为标记）；本设计负责状态机与宿主层的收敛和缺陷修复。

### 目标

- 状态机对外只剩 Linear 等价语义：待领取（agent 标签）→ 执行中 → 审核中（宿主接管至合并）→ 已完成；暂停/重试是标记不是状态。
- 人工批准只有一条路径：GitHub PR Review。删除自加的批准协议。
- 退回执行只有一个 stage；外部已合并的 PR 任何等待态都能收敛到 Done。
- 修掉已确认的状态纰漏，不引入新的等待循环。

### 非目标

- 不引入 Linear tracker；不动 GitHub issue/PR 数据模型。
- 不改变 deliver/交付收据/cycle/attempts 等已有执行契约；不改变 `[board] 继续优化` 评论协议的用户侧用法。
- 不动 HostController 的存在（独立合并、不占 AI 槽是已确认的收益，保留）。
- 不动 CI 分层、BUILD_ID、风险判定规则本身。

### 本次范围

- `python/symphony/github/workflow.py`、`host.py`、`workflow_policy.py`、`rework.py`
- `python/symphony/agent/runner.py`、`orchestrator.py`（退出原因与死路径）
- `python/symphony/http_api.py`（批准 API 移除）
- `board/server.py`（批准 UI/标签操作移除、stage 文案更新）
- `deploy/WORKFLOW.199.md`（无需新增配置）
- 相关测试的删改

### 现有能力 / 可复用内容

- `resolve_merge`、`_finish_requeue`、`locked` 收据锁、`rework.snapshot/pending`、宿主 `REVIEW_STAGES` 看板映射、`pause`/needs-human 暂停协议均保留。
- GitHub review 判定 `_approved`（commit_id == 交付 SHA + 非作者 User 审核）保留，作唯一批准来源。

### 影响范围

- 收据 schema：`host.stage` 枚举收缩；`delivery.human_approval`/`approval_after`/`approval_floor_event` 字段废弃；旧收据需读取兼容。
- 外部 API：`POST /api/v1/github/approve` 移除（协议变化，看板是唯一已知调用方）。
- 看板：批准/撤销按钮、`merge-approved` 标签展示与操作移除。
- 行为变化：高风险交付只能由 GitHub Review 放行，不再有看板按钮路径（与 SPEC/Elixir 对齐）。

## 2. 设计

### 2.1 状态与流转

目标状态集（host.stage）：

```text
执行期：          （无 host）agent 标签 = Todo；running/retrying/blocked = In Progress
接管期：          handing_off
审核中：          awaiting → waiting_checks / waiting_review / waiting_update / checking → merging → finalizing → merged
退回：            returned（合并 repair_pending + requeue_pending；_finish_requeue 处理后置 superseded）
终止/异常：       paused / closed / merged（merged 为内部终态，Issue 关闭后列 Done）
```

关键流转修正：

- `reconcile_delivery` 在处理 `returned`、`waiting_update`（rework 待提交）分支**之前**先查 `delivery.pr` 的 merged/closed 状态：merged → `pending_merge` + `resolve_merge`；closed → `closed` stage。保证任何等待态都能收敛，不依赖 Issue 是否关闭。
- `finish_handoff` 对 `state != open` 的 Issue 置 `closed`（不再统一抛 `handoff_paused`）；`needs-human` 仍置 `paused`。
- `_final_gate` 顺序：`reviewDecision == REVIEW_REQUIRED` 或无有效批准先判 `await review`，再判 `mergeStateStatus`。`waiting_review` 成为可达状态；文案改为"等待 GitHub Review"。
- `_approved` 下界只保留 `feedback_after`（交付 SHA 同一 commit 上的历史 review 有效，交付前批准的 review 不再被 `approval_after` 误排）；`approval_after`/`approval_floor_event`/`_label_approved` 整段删除。
- runner：已发布交付的 `skip`（含 `admission_only` 评估壳）统一走 `DONE_AWAIT_MERGE`，由 merge_poll 30s 重查链续命；不再落 `DONE_NORMAL`→1s continuation。`merge_eval`/`admission_only` 保留——它是 merge_poll 不占槽重查的机制；修复重派不依赖它（`_finish_requeue` 重贴 agent → `new_cycle` → 普通派发）。
- `pause()` 中 `blocked` 持久化后的标签写入（加 needs-human / 摘 agent）纳入宿主轮询的幂等修复：每轮对 `blocked` 记录核对一次标签状态，失败写 `last_error` 继续重试，不再永久跳过。
- 收据损坏（`invalid_workflow_receipt`）：begin_run 失败路径在派发层识别该原因后置 `blocked` 并摘 agent 标签（fail closed），替代无限指数退避。

stage 合并：

- `repair_pending`、`requeue_pending` 统一为 `returned`；语义不变（等待 `_finish_requeue` 恢复 agent 标签）。
- 读取兼容：旧收据中 `repair_pending`/`requeue_pending` 在 `_read_record` 归一化为 `returned`；看板 `REVIEW_STAGES` 同步改为 `returned`（兼容期间可保留旧名映射，但服务端只写新值）。

### 2.2 数据 / 兼容

- 收据：删除 `delivery.human_approval`、`delivery.approval_after`、`approval_floor_event`、`host.delete_grant` 以外不需要迁移；旧字段读取时忽略（不报错、不回写）。
- `merge-approved` 标签：宿主不再赋予任何授权语义（与 Elixir 一致）；存量 issue 上残留标签无害，看板不再展示/操作它。
- `delete_grant`/`superseded`/`previous_delivery`/`repair_budget`/`feedback_claim` 保留不变。

### 2.3 API / 交互契约

- 移除 `POST /api/v1/github/approve` 路由与 `approve_delivery`；旧调用收到 404（协议移除，看板是唯一调用方，同步删除按钮）。
- `github_workflows` 快照移除 `can_approve` 字段；`delivery_id`/`sha`/`stage`/`reason`/`error` 保留。
- 看板删除：`data-approvable`/`data-approved`/`data-delivery-id`/`data-sha`（批准专用）、批准/撤销按钮与 `approve`/`revoke` 操作分支、`has_approved`/`_clear_labels` 中 `merge-approved` 处理、`handle_label` 中 `merge-approved` 白名单（改为拒绝，避免误加无语义标签）。
- 配置：`github_workflow.approval_label` 配置项移除；`workflow_policy` 校验删除对应分支。199 未配置，无迁移成本。

### 2.4 简化清单（死代码）

- `merge_delivery`（已无调用方）、`human_approval` 只写字段、orchestrator `completed` 集合（只写不读）、看板批准相关 JS（approve/revoke 的 operationView/confirmOperations/operationNotice 分支）。

## 3. 边界与风险

- 权限 / 安全边界：批准后只有 GitHub Review 一条路径，高风险强制审核的仓库行为不变；无强制审核的仓库高风险交付需人工在 PR 上 Approve（同 Elixir）。
- 兼容性：收据 stage 改名做读取端归一化；`merge-approved` 残留标签降级为无语义；批准 API 移除属协议变化，已知调用方仅看板。
- 主要风险：`reconcile` 顺序调整后 merged 检查前置每轮多一次 PR 请求（30s/单 issue，量可接受）；`_final_gate` 顺序变化可能让原先判 fix 的交付进入等待 review——这是设计目标本身。
- 不可逆点 / 回滚：均为代码删除，无数据写入路径变化；旧收据读取兼容，回滚只需 revert 代码。

## 4. 验收与验证

| ID | 验收标准 | 验证方式 | 通过标准 |
|---|---|---|---|
| AC01 | `skip` 不再热循环 | 单测：已发布交付 + agent 标签时普通派发与 admission_only 壳的 skip 均退出为 DONE_AWAIT_MERGE | 不再产生 1s continuation；merge_poll 链 30s 一跳不占 AI 槽 |
| AC02 | `waiting_review` 可达 | 单测：mergeStateStatus=BLOCKED + REVIEW_REQUIRED → host 进入等待 review 而非返修 | stage=waiting_review，repair_budget 不消耗 |
| AC03 | 等待态收敛 merged | 单测：returned / waiting_update / checking 态下 PR 外部 merged → pending_merge → merged | 各等待态均收敛，不漏检 |
| AC04 | 批准层移除 | grep + 单测：无 approval_label/approve_delivery/can_approve；`/api/v1/github/approve` 404；看板无批准按钮与 merge-approved 写路径 | 静态+接口断言通过 |
| AC05 | 旧收据兼容 | 单测：stage=repair_pending/requeue_pending 收据读取归一化为 returned，宿主正常续走 | 兼容映射生效 |
| AC06 | 高风险仅靠 GitHub Review | 单测：高风险交付无 review → waiting_review；同 SHA APPROVED review → 放行 | 行为符合 SPEC |
| AC07 | 回归 | `cd python && python -m pytest` 全量 + `board/tests` pytest + node 测试 | 全绿 |

## 5. 确认项

### 阻塞确认项

- 无

### 非阻塞待确认项

- `waiting_update`（rework 评论已记录、等待人工提交执行请求）保留：评论协议不变，仅 digest 作幂等；若后续想进一步砍，可单独再议。
- orchestrator `blocked`（agent 标签仍在的派发级阻塞）映射到"进行中"列的展示文案维持现状。
