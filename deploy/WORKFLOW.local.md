---
tracker:
  kind: linear
  provider:
    project_slug: "$LINEAR_PROJECT_SLUG"
    api_key: "$LINEAR_API_KEY"
    agent_sessions:
      enabled: true
      status_url: http://127.0.0.1:18331/api/v1
      progress_interval_seconds: 30
      max_progress_activities: 20
      oauth:
        client_id: "$LINEAR_OAUTH_CLIENT_ID"
        client_secret: "$LINEAR_OAUTH_CLIENT_SECRET"
        scope: read,write
  active_states: [Todo, In Progress, Rework]
  terminal_states: [Closed, Cancelled, Canceled, Duplicate, Done]
polling:
  interval_ms: 10000
server:
  host: 127.0.0.1
  port: 18331
workspace:
  root: "$SYMPHONY_WORKSPACE_ROOT"
delivery:
  enabled: true
  repo: "$SYMPHONY_REPO"
  token: "$SYMPHONY_GITHUB_TOKEN"
  base_branch: main
  review_state: Human Review
  merge_state: Merging
  rework_state: Rework
  done_state: Done
  require_branch_protection: true
  allow_without_branch_protection: false
  required_approvals: 0
  max_auto_files: 10
  max_rework_attempts: 2
  poll_interval_ms: 10000
hooks:
  after_create: |
    GIT_SSH_COMMAND="ssh -i \"$SYMPHONY_DEPLOY_KEY\" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new" git clone "git@github.com:$SYMPHONY_REPO.git" .
    git config core.sshCommand "ssh -i \"$SYMPHONY_DEPLOY_KEY\" -o IdentitiesOnly=yes -o BatchMode=yes"
    git config user.name "$SYMPHONY_GIT_NAME"
    git config user.email "$SYMPHONY_GIT_EMAIL"
  timeout_ms: 300000
agent:
  max_concurrent_agents: 1
  max_turns: 30
  max_retry_attempts: 10
  max_retry_backoff_ms: 60000
runtime:
  kind: pi
  model: magpie/devin/swe-2
  thinking: max
  command: pi --mode rpc --no-extensions --skill "$SYMPHONY_SKILLS_DIR" --no-prompt-templates --approve --tools read,bash,edit,write,grep,find,ls --session-dir "$SYMPHONY_SESSIONS_DIR"
  turn_timeout_ms: 1800000
  stall_timeout_ms: 600000
  mcp:
    enabled: true
---

You are implementing Linear issue {{ issue.identifier }} in an isolated workspace.
Title: {{ issue.title }}
State: {{ issue.state }}
URL: {{ issue.url }}
Description:
{{ issue.description }}
{% if attempt %}This is attempt {{ attempt }}; resume existing work and PR.{% endif %}

- Read the current issue through `linear_graphql`; only execute Todo, In Progress,
  or Rework issues. Move Todo to In Progress before implementation.
- Maintain one persistent `## Codex Workpad` comment containing the plan,
  acceptance criteria, validation results, current commit, and blockers.
- Inspect the repository and reproduce the problem before editing. Implement only
  the requested scope, run the required tests, commit and push the branch.
- Reuse the current branch/PR for rework. Use a fresh branch if the old PR was
  already closed or merged. Do not create duplicate PRs.
- Git uses the workspace's Contents-only SSH deploy key. Never obtain another
  credential or use `gh`; create/update PRs and inspect checks/reviews through the
  host `github_api` tool. Address actionable feedback and rerun validation.
- Attach the PR to the issue with `attachmentCreate`, add the `symphony` PR label,
  and move the issue to Human Review only after the required validation is green.
- Human Review and Merging belong to the host delivery controller. Stop working
  in those states. Never merge PRs or mark the issue Done yourself.
- For a real external blocker, record exactly what is missing and why in the
  Workpad and move to Human Review. Do not request interactive assistance.
- The host owns Agent Session reporting. Do not create sessions or emit activities.
