# Symphony Python + Pi

此目录是当前 **唯一产品运行核心**：Python Orchestrator + Pi JSON-RPC Runner，
使用 OpenAI Symphony 的 **Linear Tracker / WORKFLOW.md 业务流程**。
`elixir/` 保留为上游参考实现和测试基线，不作为默认运行路径。

全 Python 核心的目标、模块矩阵和分期任务见
[`docs/python-core-parity.md`](../docs/python-core-parity.md) 与
[`work/任务/全Python核心移植/design.md`](../work/任务/全Python核心移植/design.md)。

- **Tracker**：默认主链路是 Linear 项目；同时支持 Jira Cloud、Asana、GitHub Issues
  和本地 Board。Linear 的 Issue、状态、评论、Workpad 和 PR 关联由 Linear 管理；
  Jira 通过项目 Key 和 JQL 轮询，并使用宿主侧 `jira_rest` 工具；Asana 按项目任务
  轮询并使用宿主侧 `asana_api` 工具。
- **Orchestrator**：沿用 Python 原有轮询、claimed、并发、retry、workspace 隔离和运行 API。
- **Runner**：沿用 `pi --mode rpc` 和工作区 MCP Bridge；通过宿主 `linear_graphql` 工具操作 Linear。
- **Workflow**：默认使用 `../deploy/WORKFLOW.local.md`（宿主安全交付）；
  `../deploy/WORKFLOW.md` 仅保留为不含宿主交付的上游 Linear 参考。
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
export LINEAR_ASSIGNEE='me'               # 可选；也可在 tracker.assignee 中指定
python -m symphony --workflow ../deploy/WORKFLOW.local.md --check
python -m symphony --workflow ../deploy/WORKFLOW.local.md `
  --i-understand-that-this-will-be-running-without-the-usual-guardrails
```

Linear 未显式配置 `active_states` / `terminal_states` 时，使用原版默认值：
`Todo, In Progress` 与 `Closed, Cancelled, Canceled, Duplicate, Done`。

## SSH Worker（可选）

不配置 `worker.ssh_hosts` 时 Pi 在宿主本地运行。配置后，每个 Issue 会固定分配到
一个远程主机，同一 worker lifetime 不会跨机器；主机容量由
`worker.max_concurrent_agents_per_host` 限制。

```yaml
worker:
  ssh_hosts:
    - "deploy@worker-a:2222"
    - "worker-b"
  max_concurrent_agents_per_host: 2
```

远程行为与原版一致：

- workspace 仍是 `<workspace.root>/<workspace_key>`，`~` 由远程 shell 展开；
  创建、Hooks 和清理都通过 `ssh ... "bash -lc '<command>'"` 执行。
- `SYMPHONY_SSH_CONFIG` 可指定 OpenSSH 配置文件；`host:port` 简写会被拆成
  `-p`，裸 IPv6 地址不会被误拆。
- MCP bridge 仍在宿主 `127.0.0.1`。远程 Pi 通过
  `ssh -R 127.0.0.1:<bridge-port>:127.0.0.1:<bridge-port>` 访问；设置
  `ExitOnForwardFailure=yes`，远程端口冲突时直接拒绝启动，不会静默退化为无工具执行。
- 远程 lease 保存在宿主本地状态目录；可用 `SYMPHONY_LEASE_ROOT` 指定，默认是
  系统临时目录下的 `.symphony-run-locks`。不要在远程 workspace root 下创建锁。
- 远程 Hooks 在远程用户的登录 shell 中执行，宿主环境变量不会自动转发。
  `SYMPHONY_DEPLOY_KEY`、`SYMPHONY_REPO`、Git 身份等必须在远程主机配置，
  或改用远程主机已有的 SSH agent/凭据；宿主 Linear/GitHub 凭据仍会被剥离。
- `stop_session` 确认的是本地 SSH 客户端进程树；远程 Pi 依赖 SSH 断连后的 SIGHUP，
  未做远程进程扫描。这与原版关闭 SSH 端口的边界一致。
- 远程 `.pi/mcp.json` 含 worker bearer，权限为 `0600`；远程主机必须是可信执行环境。

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

### Jira Cloud

```yaml
tracker:
  kind: jira
  provider:
    base_url: "https://your-org.atlassian.net"
    email: "$JIRA_EMAIL"
    api_token: "$JIRA_API_TOKEN"
    project_key: "SYM"
  active_states: ["To Do", "In Progress"]
  terminal_states: ["Done"]
```

Jira 使用 `/rest/api/3/search/jql` 分页读取活跃状态 Issue，使用
`/rest/api/3/issue/bulkfetch` 按 ID 刷新 blockers，并把 ADF 描述转成纯文本。
`jira_rest` 仅允许 `/rest/api/3/` 路径和 GET/POST/PUT/DELETE，凭据只保留在宿主。

### GitLab

```yaml
tracker:
  kind: gitlab
  provider:
    api_url: "https://gitlab.com/api/v4"   # 可选
    project_path: "group/project"
    api_key: "$GITLAB_PAT"
  active_states: ["opened"]
  terminal_states: ["closed"]
```

GitLab 按项目分页读取 Issue，`opened` / `closed` 会映射为 GitLab 状态过滤，
按 IID 刷新时 404 会跳过；`gitlab_api` 仅允许相对路径和 GET/POST/PUT/DELETE，
凭据只保留在宿主。也支持 `GITLAB_PROJECT_PATH`、`GITLAB_PAT` 环境变量回退。

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
- GitHub Issues 和本地 Board 仍是受支持的辅助 Tracker；默认部署主链路为
  Linear + Pi。

## 验证

```bash
cd python
python -m unittest discover -s tests -p 'test_linear.py'
python -m unittest discover -s tests -p 'test_jira.py'
python -m unittest discover -s tests -p 'test_ssh.py'
python -m unittest discover -s tests -p 'test_workspace_remote.py'
python -m unittest discover -s tests -p 'test_pi_remote.py'
python -m unittest discover -s tests
```
