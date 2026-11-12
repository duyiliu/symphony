# Symphony Python + Pi（Linear Tracker）

此目录保留原 Fork 的 **Python Orchestrator + Pi JSON-RPC Runner**，使用
OpenAI Symphony 的 **Linear Tracker / WORKFLOW.md 业务流程**。

- **Tracker**：Linear 项目中的 Issue，状态、评论、Workpad 和 PR 关联由 Linear 管理。
- **Orchestrator**：沿用 Python 原有轮询、claimed、并发、retry、workspace 隔离和运行 API。
- **Runner**：沿用 `pi --mode rpc` 和工作区 MCP Bridge；通过宿主 `linear_graphql` 工具操作 Linear。
- **Workflow**：`../deploy/WORKFLOW.md` 的正文直接沿用官方 Linear 示例；配置改为 Python/Pi。

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

## 运行安全

- 运行进程与 MCP bridge 持有 Linear 凭据，Pi 子进程环境移除 `LINEAR_API_KEY`。
- `linear_graphql` 是与官方相同的原生 GraphQL 工具：**凭据权限可能覆盖多个项目**，
  项目级读取限制不等于工具写权限隔离；应使用权限最小的身份和受控主机。
- 本 PR 是回归官方 Linear 流程的迁移，**尚未提供真实 Linear+Pi E2E 测试证据**；
  先在测试项目验证状态更新、CI、PR 交接及重启恢复，再投入无人值守。
- 旧 Python GitHub/Board 模块暂时仍存于 `python/symphony/`，但新的部署 Workflow
  不再使用，后续可在单独清理提交中移除并修整历史测试。

## 验证

```bash
cd python
python -m unittest discover -s tests -p 'test_linear.py'
python -m unittest discover -s tests
```
