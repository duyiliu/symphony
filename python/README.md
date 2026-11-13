# Symphony Python + Pi（Linear Tracker）

此目录保留原 Fork 的 **Python Orchestrator + Pi JSON-RPC Runner**，使用
OpenAI Symphony 的 **Linear Tracker / WORKFLOW.md 业务流程**。

- **Tracker**：Linear 项目中的 Issue，状态、评论、Workpad 和 PR 关联由 Linear 管理。
- **Orchestrator**：沿用 Python 原有轮询、claimed、并发、retry、workspace 隔离和运行 API。
- **Runner**：沿用 `pi --mode rpc` 和工作区 MCP Bridge；通过宿主 `linear_graphql` 工具操作 Linear。
- **Workflow**：`../deploy/WORKFLOW.md` 的正文直接沿用官方 Linear 示例；配置改为 Python/Pi。
- **Delivery Gate**：可选的宿主侧安全交付控制器，读取 Linear PR 关联、当前 PR SHA 的 CI、
  Review、分支保护与风险路径，通过后由宿主执行合并，再回写 Linear。

> 此次切换不意味着已有 GitHub Issues 自动变成 Linear Issue。需先在 Linear 中确认
> GitHub Issues Sync 或历史导入，再将目标 Linear 项目 slug 配置到 Workflow。
> 官方示例是 Human Review → Merging 的人工审核流程，并非默认无人值守合并。

## 配置

```bash
cd python
pip3 install -r requirements.txt
export LINEAR_API_KEY='...'                # 存在运行环境中，不提交仓库
export LINEAR_PROJECT_SLUG='your-slug'    # Linear 项目 URL 中的 slug
python -m symphony --workflow ../deploy/WORKFLOW.md --check
python -m symphony --workflow ../deploy/WORKFLOW.md
```

部署 Workflow 的 `hooks.after_create` 当前克隆
`duyiliu/scenario-test`；如果 Linear 项目对应其他代码仓库，**先修改这里再启动**。
`runtime.kind: pi` 可保持原有模型及 RPC 参数；使用实际部署的 Pi 凭据/默认模型。

## 核心调用关系

```text
Linear Project
  -> linear adapter (GraphQL poll / ID refresh)
  -> Python Orchestrator (poll, claim, retry, reconcile)
  -> Python workspace + Pi JSON-RPC
  -> MCP bridge -> linear_graphql (host-side LINEAR_API_KEY)
  -> WORKFLOW.md agent handoff
```

Linear Adapter 根据项目 slug、可选 assignee、状态、required labels 和未完成 blockers
决定派发；Merging/Review 等状态仍由官方 Workflow 处理。

## 安全自动交付（可选）

在 Workflow 中增加 `delivery` 段后，宿主会轮询处于 `review_state` / `merge_state`
的 Linear Issue，并从 Issue attachment 中读取 GitHub PR：

```yaml
delivery:
  enabled: true
  repo: duyiliu/symphony-e2e
  token: "$SYMPHONY_GITHUB_TOKEN"   # 仅宿主可读，绝不传给 Pi
  base_branch: main
  review_state: Human Review
  merge_state: Merging
  rework_state: Rework
  done_state: Done
  allow_auto_merge: true
  require_branch_protection: true
  required_approvals: 0
  max_auto_files: 10
  max_rework_attempts: 2
  risk_paths:
    - "**/*.sql"
    - "**/auth/**"
    - ".github/workflows/**"
  risk_labels: [high-risk, security, database]
```

安全边界：

- `review_state` 和 `merge_state` 不能出现在 `tracker.active_states` 中，否则启动失败；
  合并由宿主控制，不能让 Pi 在 `Merging` 状态再次运行 `land`。
- 宿主必须拿到 PR 当前真实 HEAD SHA，并只接受该 SHA 的 check-runs / commit statuses、
  Review 和合并结果；新 commit 会重新跑全部门禁。
- 无 CI、CI 失败、冲突、`CHANGES_REQUESTED`、缺少必需 Review、分支保护不可用或
  风险路径命中时，默认暂停并回到 `Human Review`，不会合并。
- `required_approvals` 与 GitHub 分支保护中的必需 Review 数取更严格结果；测试环境
  若暂时无法启用分支保护，必须显式设置 `allow_without_branch_protection: true`，
  生产环境不应这样做。
- 自动返工只会在失败原因和 HEAD SHA 变化时重新排队，最多 `max_rework_attempts` 次；
  达到上限或返工没有产生新 SHA 时暂停，等待人工处理。
- 宿主合并后必须重新读取 GitHub 的 `merged` 状态且确认 HEAD SHA 未变化，再更新
  Linear 为 `done_state`。任何无法确认的合并结果都按失败暂停。

Pi 不应持有可合并的 GitHub 凭据。推荐使用只读/写 Contents 的部署密钥或细粒度
Token 推送分支，PR 创建通过宿主 `github_api` 工具完成；该工具的 allowlist 明确
禁止 `PUT /pulls/{n}/merge`。`runtime.mcp.enabled` 必须为 `true` 才能使用该工具。

## 运行安全

- 运行进程与 MCP bridge 持有 Linear 凭据，Pi 子进程环境移除 `LINEAR_API_KEY`。
- `linear_graphql` 是与官方相同的原生 GraphQL 工具：**凭据权限可能覆盖多个项目**，
  项目级读取限制不等于工具写权限隔离；应使用权限最小的身份和受控主机。
- 回归官方 Linear 流程的迁移已在测试 Linear 项目验证自动领取、Pi 执行、PR 创建、
  失败重试上限、取消收敛和 PR 复用；安全合并与重启恢复需按
  `docs/linear-safe-delivery.md` 的验证清单执行。
- 旧 Python GitHub/Board 模块暂时仍存于 `python/symphony/`，但新的部署 Workflow
  不再使用，后续可在单独清理提交中移除并修整历史测试。

## 验证

```bash
cd python
python -m unittest discover -s tests -p 'test_linear.py'
python -m unittest discover -s tests
```
