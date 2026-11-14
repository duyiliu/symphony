# 原版 ExUnit → Python 测试映射

基线：`upstream/main` = `be10a1b79df723d6d7612b5651c8522704dafb2e`

状态说明：

- **已覆盖**：Python 测试断言同一行为。
- **替代覆盖**：结构不同（Pi 替代 Codex、board 替代 Phoenix），但对外行为有测试。
- **未覆盖**：当前没有等价测试，已标注所属阶段。
- **不适用**：Elixir/OTP 或已退役能力。

## core_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| config defaults and validation checks | `test_workflow_config.ConfigTest.test_parse_settings` / `test_defaults` / `test_invalid_max_concurrent` / `test_invalid_max_retry_attempts` | 已覆盖 |
| current WORKFLOW.md valid | `test_template_issue_paths.TemplateTest.test_deploy_workflow_shape` | 已覆盖 |
| linear api token / assignee env resolution | `test_workflow_config.ConfigTest.test_env_indirection`；`test_linear.LinearTrackerTests.test_linear_config_from_provider` | 已覆盖 |
| workflow file path defaults / app env override | `test_logging_cli.CliParserTest.test_resolve_workflow_prefers_explicit_path` / `test_resolve_workflow_uses_env_override` / `test_resolve_workflow_falls_back_to_existing_default` | 已覆盖 |
| prompt-only / unterminated front matter / non-map front matter | `test_workflow_config.WorkflowTest.test_no_front_matter` / `test_unterminated_front_matter_keeps_config_and_empty_prompt` / `test_front_matter_not_map` | 已覆盖 |
| start_link starts runtime / startup preflight | `test_linear_pi_loop.LinearPiLoopTest.test_todo_claim_runs_pi_and_releases_after_review_state`；`test_workflow_config.WorkflowRuntimeValidationTest.test_pi_requires_command` / `test_codex_is_rejected` | 替代覆盖 |
| runtime restart keeps last good settings | `test_workflow_config.WorkflowStoreTest.test_reload_failure_keeps_last_good_settings` | 已覆盖 |
| restart does not overlap redispatched work | `test_linear_pi_loop.LinearPiLoopTest.test_repeated_refresh_while_running_does_not_claim_twice` | 已覆盖 |
| reconcile no running / non-active stop / terminal stop / recorded workspace cleanup | `test_orchestrator_units.ReconciliationTest`；`RunningRefreshTest`（issue 消失、刷新失败保留、活跃刷新）；`test_terminal_cleanup_uses_recorded_workspace` | 已覆盖 |
| reassigned / required label removed / blocked release / retry claim release | `test_reassigned_issue_stops_without_cleanup` / `test_required_label_removed_stops_running_agent` / `test_required_label_removed_releases_blocked_issue` / `test_retry_releases_claim_when_label_removed` / `test_retry_releases_claim_when_issue_disappears` | 已覆盖 |
| agent runner stops after label removed | `test_orchestrator_units.RunnerContinuationTest.test_inactive_issue_ends_after_one_turn`；`test_required_label_removed_stops_running_agent` | 已覆盖 |
| normal exit continuation / abnormal retry / first retry delay / stale retry timer | `test_done_normal_schedules_continuation_check`；`test_retry_agent_down_schedules_within_limit`；`test_exponential_backoff`；`test_stale_retry_token_is_ignored` | 已覆盖 |
| manual refresh coalescing | `test_orchestrator_units.ReconcileParityTest.test_manual_refresh_coalesces_repeated_requests`；`test_http_api.HttpApiTest.test_refresh_queues_and_returns_202` | 已覆盖 |
| SSH host selection / capacity / preferred host | `test_orchestrator_units.CandidateTest` 的 worker host 选择与 `max_concurrent_agents_per_host` | 已覆盖 |
| fetch issues by states empty | `test_linear.LinearTrackerTests.test_empty_states_skip_request`；`test_jira.FetchTest.test_empty_states_skips_request` | 已覆盖 |
| prompt issue/attempt / datetime / nested values / strict / invalid template / default template | `test_template_issue_paths.TemplateTest`；`test_liquid_filters.py`（filters、assign/capture/case/raw） | 已覆盖 |
| continuation guidance for retries | `test_template_issue_paths.TemplateTest.test_output`（`attempt` 条件段） | 已覆盖 |
| agent runner keeps workspace / timestamped updates / ssh startup failure | `test_workspace_hooks.WorkspaceHookTest.test_existing_workspace_is_reused_without_after_create`；`test_turn_completed_usage`（事件时间戳）；`test_pi_remote.McpTunnelTest.test_ssh_start_failure_revokes_scope` | 已覆盖 |
| continuation while active / max_turns | `test_orchestrator_units.RunnerContinuationTest.test_active_issue_continues_to_next_turn`；`test_linear_pi_loop` 的 `max_turns: 1` 闭环 | 已覆盖 |
| app server startup command / args / approval payload | `test_linear_pi_loop` 的真实 Pi 启动；`test_mcp_bridge.McpBridgeScopeTests` | 替代覆盖 |

## workspace_and_config_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| workspace bootstrap / deterministic path / reuse / stale path | `test_workspace_hooks.WorkspacePathTest`（确定性、collision、复用、stale 替换） | 已覆盖 |
| relative root / symlink escape / root protection | `test_workspace_hooks.WorkspacePathTest.test_relative_root_resolves_from_workflow_directory` / `test_symlink_escape_under_root_is_rejected` / `test_remove_rejects_workspace_root_itself` / `test_remove_recorded_rejects_symlink_escape_before_hooks` | 已覆盖 |
| after_create / before_run / after_run / before_remove | `test_workspace_hooks.WorkspaceHookTest`（失败、超时、大输出、清理继续、悬空链接） | 已覆盖 |
| cleanup missing root / non-binary identifier / remove all workspaces | `test_parity_fixes.RemoveIssueWorkspacesTest`（根缺失、非字符串 identifier、多 identifier、空列表、远程逐主机） | 已覆盖 |
| tracker issue helpers / required labels | `test_template_issue_paths.IssueTest.test_routable` | 已覆盖 |
| linear blockers / malformed / unassigned / pagination / non-200 logging | `test_linear.LinearTrackerTests.test_poll_pagination_and_mapping` / `test_todo_blocker_and_assignee` / `test_id_refresh_is_project_scoped_and_ordered` / `test_invalid_pagination_fails_closed` / `test_malformed_poll_rows_are_dropped_with_warning` / `test_state_names_are_stringified_and_deduplicated` | 已覆盖 |
| linear graphql bound snapshot | `test_linear.LinearTrackerTests.test_worker_can_read_only_bound_issue` / `test_tools_use_frozen_scope_not_live_workflow_store` | 已覆盖 |
| dispatch sort / blocked / assignee / labels / revalidation | `test_orchestrator_units.SortTest.test_sort_by_priority_then_created`；`CandidateTest.test_candidate` / `test_should_dispatch_claims`；`DispatchRevalidationTest`（消失、终态、刷新失败、刷新成功） | 已覆盖 |
| config defaults / env / schema / per-state concurrency | `test_workflow_config.ConfigTest.test_parse_settings` / `test_defaults` / `test_env_indirection`；`test_per_state_concurrency_limit` | 已覆盖 |
| path safety invalid segments | `test_template_issue_paths.PathTest.test_safe_join` | 已覆盖 |
| remote workspace lifecycle | `test_workspace_remote.py`（17 项：路径、创建 marker、hooks、清理、lease） | 已覆盖 |

## orchestrator_status_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| snapshot timeout / last update / session id / thread totals / turn usage | `test_orchestrator_units.SnapshotShapeTest`；`TokenDeltaTest.test_turn_completed_usage`；`test_runtime_health.HealthTest`；`test_http_api`（快照超时、running 详情） | 已覆盖 |
| token-count cumulative / rate-limit payloads | `test_orchestrator_units.TokenDeltaTest`（含 `test_rate_limit_payload_is_exposed_in_snapshot`） | 已覆盖 |
| retry backoff entries / poll countdown / immediate poll | `test_runtime_health.HealthTest.test_failed_poll_is_visible_and_next_success_recovers`；`test_orchestrator_units.RetryDelayTest`；`test_tick_enqueues_run_poll_cycle` | 已覆盖 |
| stalled worker restart / MCP elicitation blocked / input required blocked | `test_orchestrator_units.StallTest.test_stall_restarts_with_retry`；`test_turn_input_required_blocks_instead_of_retrying`；`test_agent_sessions.ReporterTest` 的 elicitation | 已覆盖 |
| terminal dashboard rendering / TPS graph / event humanization | `test_status_dashboard.py`（16 项；5 个原版快照逐字节一致） | 已覆盖 |
| rotating file logger | `test_runtime_health.FileLoggingTest.test_utf8_log_written_without_shell_redirection` | 已覆盖 |

## app_server_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| workspace root / outside root / symlink cwd rejection | `test_template_issue_paths.PathTest.test_safe_join`；`test_workspace_hooks.WorkspacePathTest` | 已覆盖（本地路径） |
| turn timeout / stall | `test_pi_runtime.TurnTimeoutTest.test_turn_timeout_is_reset_by_stream_activity`；`test_orchestrator_units.StallTest.test_pi_uses_runtime_stall_timeout` | 已覆盖（静默超时语义） |
| sandbox policy passthrough | 无 | 不适用（Pi 无 Codex sandbox） |
| request-for-input / MCP elicitation / approvals | `test_pi_runtime.TurnEventTest.test_dialog_ui_request_blocks_the_turn`；`test_orchestrator_units.RetryLimitTest.test_turn_input_required_blocks_instead_of_retrying` | 替代覆盖（Pi 扩展 UI → blocked）；人工答案回传仍未实现 |
| dynamic tool calls / unsupported / tool failure | `test_mcp_bridge.McpBridgeScopeTests`；`test_linear.LinearTrackerTests` 工具测试 | 已覆盖 |
| partial JSON lines / malformed events / side output | `test_pi_runtime.TurnEventTest.test_malformed_and_side_output_lines_are_ignored`；`test_pi_runtime.ReaderLoopTest.test_partial_final_line_is_delivered_without_newline` | 已覆盖 |
| credential stripping | `test_pi_runtime.ScriptIsolationTest`；`test_linear_oauth.OAuthTest.test_oauth_environment_names_are_removed_from_workers`；`test_mcp_bridge.McpBridgeScopeTests.test_client_config_contains_scoped_bearer_not_host_tokens` | 已覆盖 |
| SSH launch | `test_pi_remote.py`（远程脚本、MCP 反向隧道、启动失败撤销 scope） | 已覆盖 |

## dynamic_tool_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| linear_graphql spec / unsupported / bound snapshot | `test_linear.LinearTrackerTests.test_pi_exposes_only_linear_tool` / `test_worker_can_read_only_bound_issue` / `test_tools_use_frozen_scope_not_live_workflow_store` | 已覆盖 |
| raw query / legacy operationName / multi-operation | `test_linear.LinearTrackerTests.test_tool_trims_query_and_validates_variables` | 部分覆盖；多 operation 被 AST allowlist 明确拒绝，属于安全增强 |
| blank query / GraphQL errors / argument validation / transport failures | `test_linear.LinearTrackerTests.test_tool_trims_query_and_validates_variables`；`test_mcp_bridge` | 部分覆盖；GraphQL 错误保留机器可读 `code/detail` |
| non-JSON payload fallback | 无 | 不适用；Python 工具统一 JSON 输出，非对象 payload 返回 `linear_unknown_payload` |

## github_adapter_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| adapter validation / reads / github_api spec | `test_github_adapter.ValidateConfigTest`；`test_github.ExecuteAgentToolTest`；`test_delivery.AgentToolBoundaryTest` | 已覆盖 |
| provider settings / token env | `test_workflow_config.ConfigTest.test_env_indirection`；`test_github`；`test_github_adapter` | 已覆盖 |
| normalize / page / refresh / malformed | `test_github.NormalizeTest` / `ResponseMetadataTest`；`test_github_adapter.FetchTest`；`test_github_tasks` | 已覆盖 |
| github_api unsafe args / status / failures | `test_github.ExecuteAgentToolTest`；`test_delivery.AgentToolBoundaryTest` | 已覆盖 |
| tracker binds GitHub tools | `test_delivery.AgentToolBoundaryTest.test_linear_agent_gets_github_tool_only_when_delivery_enabled` | 已覆盖 |

## jira_adapter_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| adapter validation / reads / jira_rest spec | `test_jira.AdapterValidationTest` / `TrackerFacadeTest` | 已覆盖 |
| provider settings / token env | `test_jira.ClientSettingsTest` | 已覆盖 |
| ADF / labels / blockers / dispatchable | `test_jira.NormalizeIssueTest` / `BlockerTest` | 已覆盖 |
| enhanced search pagination / malformed / missing token | `test_jira.FetchTest` | 已覆盖 |
| bulkfetch order / project scope / malformed | `test_jira.FetchTest` | 已覆盖 |
| jira_rest status / unsafe args / failures | `test_jira.AgentToolTest` | 已覆盖 |
| tracker binds Jira tools | `test_jira.TrackerFacadeTest` | 已覆盖 |

## asana_adapter_test.exs / gitlab_adapter_test.exs

| 原版测试文件 | Python 对应 | 状态 |
|---|---|---|
| `asana_adapter_test.exs` | `test_asana.py`（25 项） | 已覆盖 |
| `gitlab_adapter_test.exs` | `test_gitlab.py`（16 项） | 已覆盖 |

## ssh_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| IPv6 / host:port / user@host / ssh unavailable | `test_ssh.ParseTargetTest` / `RunTest.test_missing_ssh` | 已覆盖 |
| start_port line/binary mode / remote shell quoting | `test_ssh.StartPortTest` / `RemoteShellCommandTest` / `SshArgsTest` | 已覆盖；Python 使用管道流式而非 Erlang Port |

## cli_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| guardrail banner / workflow default / explicit path | `test_logging_cli.CliParserTest`（显式、环境变量、默认路径、guardrail 提示与跳过 `--check`） | 已覆盖（Pi 文案适配） |
| --logs-root / missing workflow / startup error / ok | `test_logging_cli.CliParserTest`（位置参数、`--port`、`--check`、`--logs-root`、`--log-file`） | 已覆盖（参数解析；启动错误路径由 CLI 返回码实现，未单独集成测试） |

## log_file_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| default log path / custom root | `test_logging_cli.LoggingSetupTest.test_default_log_file_matches_elixir_layout` / `test_setup_uses_utf8_rotating_handler` | 已覆盖 |

## observability_pubsub_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| subscribe / broadcast / unavailable no-op | `board/tests/test_server.py`（HTTP 轮询替代） | 替代覆盖；PubSub 不适用 |

## status_dashboard_snapshot_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| idle / busy / backoff / credits fixtures | `test_status_dashboard.SnapshotTest`（idle、idle+dashboard url、super_busy、backoff_queue、credits_unlimited 逐字节比对） | 已覆盖 |

## extensions_test.exs

| 原版测试主题 | Python 对应 | 状态 |
|---|---|---|
| workflow store reload / last good / missing file | `test_workflow_config.WorkflowStoreTest.test_reload_failure_keeps_last_good_settings`；`WorkflowTest.test_missing_file` | 已覆盖 |
| tracker delegates to adapters | `test_linear` / `test_github` / `test_jira` | 已覆盖 |
| observability API state/issue/refresh/405/404/503 | `test_http_api.HttpApiTest`（9 项） | 已覆盖 |
| dashboard LiveView / static assets / host checks | `board/tests/test_server.py` | 替代覆盖 |

## 剩余未覆盖与有意差异

Phase 1 的原版核心测试映射已全部有对应测试；以下为仍未覆盖或有意不同的项：

- **人工答案/停止信号回传 Pi**：Pi 扩展 UI 对话框进入 blocked，但答案不能注入
  现有 session；停止通过终止进程树实现。属于 Pi Runtime 边界，见
  `docs/local-agent-sessions.md`。
- **`linear_graphql` 多 operation**：AST allowlist 明确拒绝，属于安全增强。
- **GraphQL 错误载荷**：Python 保留机器可读 `code/detail`，文本格式与原版不同。
- **Codex sandbox policy**：Pi 无 Codex sandbox，属于有意差异。
- **Phase 3 现场验证**：SSH Worker 已有 `test_ssh.py`、`test_workspace_remote.py`、
  `test_pi_remote.py`；真实远程主机和 Jira/Asana/GitLab live e2e 仍需现场执行。
