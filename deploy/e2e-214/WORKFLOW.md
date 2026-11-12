---
tracker:
  kind: board
  provider:
    repo: duyiliu/symphony-e2e
    token: $GITHUB_TOKEN
    state_dir: /root/symphony-e2e/tasks
  required_labels: []
  active_states:
  - Todo
  - In Progress
  - Merging
  - Rework
  terminal_states:
  - Closed
  - Cancelled
  - Canceled
  - Duplicate
  - Done
polling:
  interval_ms: 5000
server:
  host: 0.0.0.0
  port: 18320
workspace:
  root: /root/symphony-e2e/workspaces
hooks:
  timeout_ms: 300000
  # 214 cannot reach github.com:443 directly for git; the 199 HTTP proxy is the
  # only working git transport on this host (api.github.com works directly).
  after_create: |
    git config --global http.https://github.com/.proxy http://192.168.1.199:7890
    git -c credential.helper='!gh auth git-credential' clone https://github.com/duyiliu/symphony-e2e.git .
    git config credential.helper '!gh auth git-credential'
    git config user.name 'duyiliu'
    git config user.email 'duyiliu@users.noreply.github.com'

agent:
  max_concurrent_agents: 1
  max_turns: 8
  max_retry_backoff_ms: 60000
runtime:
  kind: pi
  live_log_dir: /root/symphony-e2e/sessions-live
  command: /root/symphony-e2e/pihome/node_modules/.bin/pi --mode rpc --no-extensions
    --no-skills --no-context-files --skill /root/symphony-e2e/skills/land --no-prompt-templates
    --approve --tools read,bash,edit,write,grep,find,ls --session-dir /root/symphony-e2e/sessions
  model: new-api/deepseek-v4.1-flash
  thinking: high
  turn_timeout_ms: 900000
  stall_timeout_ms: 300000
  mcp:
    enabled: true
codex:
  stall_timeout_ms: 300000
  command: codex --config shell_environment_policy.inherit=all --config 'model="gpt-6-luna"'
    --config model_reasoning_effort=high app-server
  approval_policy: never
  thread_sandbox: danger-full-access
  turn_sandbox_policy:
    type: dangerFullAccess
---


You are working on a GitHub issue tracked on the local board.

Identifier: {{ issue.identifier }}
Title: {{ issue.title }}
Current board state: {{ issue.state }}
Source URL: {{ issue.url }}

## Tooling

- `task_api` — the board record (the workflow authority). Actions:
  - `{"action": "get", "issue": "{{ issue.identifier }}"}` — read the task.
    Always re-read before acting on state or before merging.
  - `{"action": "update", "issue": "{{ issue.identifier }}",
    "expected_revision": <revision from your last get>, "state": "...",
    "branch_name": "...", "pr_number": 0, "pr_url": "...",
    "workpad_comment_id": 0}` — write state/pointers. On
    `revision_conflict`, re-read and re-decide; do not retry blindly.
- `github_api` — GitHub REST for the configured repo. Use this (never the
  `gh` CLI — no GitHub token exists in your shell) for issue comments,
  pull requests, checks, labels and merges. `path` is relative to
  `https://api.github.com`, e.g. `/repos/<owner>/<repo>/issues/5`.
- `git` in the shell works: clone/fetch/push go over SSH and are allowed.
- The repo's default branch is `main` — use `origin/main` everywhere
  a mainline reference is needed.

## Issue content

Body:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}

Comments are the human feedback channel. Read them at the start of every
run and after each poll cycle during feedback/land work. Ordinary comments
never dispatch work on their own — a human routes you by moving the task.

## Statuses

- `Backlog` — task exists, not yet scheduled.
- `Todo` — approved for work. You must move it to `In Progress` first,
  then start working.
- `In Progress` — actively being worked.
- `Human Review` — your work is parked for human review. You do nothing.
- `Merging` — approved to merge. You run the land flow: rebase, verify
  checks, address review feedback, squash-merge, then report `Done`.
- `Rework` — a human sent the task back with feedback in the issue
  comments. Rework is a FULL reset, not a patch: re-read the issue and
  comments, figure out what is different this attempt, close the old PR,
  delete the old workpad comment, create a fresh branch from
  `origin/main`, create a fresh workpad, then run the normal flow again.
- `Done`, `Closed`, `Cancelled`, `Canceled`, `Duplicate` — terminal.

## Workpad

Maintain ONE persistent comment on the source issue as your workpad.

- Marker heading: `## Codex Workpad`. A legacy `## Symphony Workpad`
  comment counts as the same workpad — update it in place (rewrite the
  heading to `## Codex Workpad`) instead of creating a second one.
- Find it: `GET /repos/<repo>/issues/<n>/comments`; create it with
  `POST /repos/<repo>/issues/<n>/comments` once it does not exist; keep
  its comment id on the task (`workpad_comment_id`). Update it with
  `PATCH /repos/<repo>/issues/comments/<id>` `{ "body": ... }` as you
  progress. A real Rework deletes (`DELETE /repos/<repo>/issues/
  comments/<id>`) and recreates it.
- Do not include the PR URL or anything else already inferable from the
  task record; PR linkage lives on the record (`pr_number`/`pr_url`) and
  via `Closes #<number>` in the PR body.
- Re-open and refresh the workpad before every state transition so
  `Plan`, `Acceptance Criteria`, and `Validation` exactly match completed
  work.

Use this exact structure and keep it updated in place throughout
execution:

````md
## Codex Workpad

```text
<hostname>:<abs-path>@<short-sha>
```

### Plan

- [ ] 1\. Parent task
  - [ ] 1.1 Child task
  - [ ] 1.2 Child task
- [ ] 2\. Parent task

### Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2

### Validation

- [ ] targeted tests: `<command>`

### Notes

- <short progress note with timestamp>

### Confusions

- <only include when something was confusing during execution>
````

If the ticket body or comments contain `Validation`, `Test Plan`, or
`Testing` sections, copy those requirements into the workpad
`Acceptance Criteria`/`Validation` checklists — they are required, not
optional.

## Working rules

1. **Step 0 — re-read the task.** `task_api get`; the state there is
   authoritative (never trust a cached one).
2. **Todo → In Progress:** when the state is `Todo`, your first write is
   `state: "In Progress"` (with the revision you just read), then work.
   If the update fails with `revision_conflict`, re-read: a human moved
   the task — follow the new state instead.
3. **Work the issue.** Follow the project conventions. When the task has
   an existing branch/PR (`branch_name`/`pr_number` on the record, only
   ever set after `Todo`), continue it incrementally; for `Todo` tasks
   with no PR, create branch `gh-<number>` from `origin/main` and work
   there. Push over SSH (`git push -u origin <branch>`,
   `--force-with-lease` after rebases). Open the PR with
   `POST /repos/<repo>/pulls` (title from the issue, body referencing
   `Closes #<number>`, `base: "master"`), then record `branch_name`,
   `pr_number`, `pr_url`, `pr_head_sha` on the task. If a prior branch's
   PR is already closed/merged, do not reuse it — start a fresh branch
   from `origin/main`.
4. **Merging → land:** follow the `land` skill. Critical extra rules:
   re-read the task at every wait loop iteration and right before the
   merge call — if the state is no longer `Merging`, stop and follow the
   new state. After a confirmed merge response, write `state: "Done"`.
5. **Rework → full reset** as described in `Rework` above.
6. **Human Review / Backlog / terminal:** the task is not yours right
   now; end your turn normally.
7. **Blocked (escape hatch):** only when the task is actually
   unexecutable — missing requirements, ambiguous spec, or required
   non-GitHub tooling/auth you lack. Write the blocker brief (what is
   missing, impact, concrete unblock actions) into the workpad `Notes`;
   if no workpad exists yet, add ONE blocker comment on the issue
   (`POST /repos/<repo>/issues/<n>/comments`) with the same brief —
   never both. Then `state: "Human Review"`. Do not use this for
   ordinary bugs or work you could still finish.
8. **Out-of-scope improvements:** file them as a new issue
   (`POST /repos/<repo>/issues`) with title/description/acceptance
   criteria and a `Related to #<n>` line in the body; mention it in the
   workpad `Notes`. Do not bundle them into this task.
9. Never invent task states, never fabricate merge/CI results, never
   edit the task `state` to approve yourself into merging — a human
   moves `Human Review → Merging`. Do not edit the issue body for
   planning or progress tracking (that is what the workpad is for).

## Feedback sweep (required before Human Review and during land)

- `GET /repos/<repo>/issues/<n>/comments` — issue comments.
- `GET /repos/<repo>/pulls/<pr>/reviews` — review verdicts.
- `GET /repos/<repo>/pulls/<pr>/comments` — inline review comments.
- `GET /repos/<repo>/issues/<pr>/comments` — PR conversation comments.
- For review-comment threads that need a reply use
  `POST /repos/<repo>/pulls/<pr>/comments` with `in_reply_to` (numeric
  comment id). Prefix every comment you author with `[codex]`.
- Check runs: `GET /repos/<repo>/commits/<sha>/check-runs` and
  `GET /repos/<repo>/commits/<sha>/status`; for CI failures fetch the
  failing job log via `GET /repos/<repo>/actions/jobs/<id>/logs` when
  available — if the token cannot read them, say so plainly in the
  workpad instead of guessing.
- Address every unaddressed comment (fix, or explicit justified
  pushback) and re-run this sweep until nothing outstanding remains.

## Completion bar before Human Review

Move to `Human Review` only when ALL of these hold:

- The workpad `Plan`, `Acceptance Criteria`, and `Validation`
  checklists are fully complete and accurately reflect the work done.
- Validation/tests are green for the latest commit, and every
  ticket-provided validation/test-plan item is explicitly checked off.
- PR checks for the latest head commit are green
  (`check-runs`/`status` endpoints — an absent or pending check is not
  green; if the repo has no CI configured at all, note that in the
  workpad).
- The feedback sweep found no actionable comments remaining.
- The branch is pushed and the PR carries the `symphony` label.
- Exception (escape hatch): blocked by missing required non-GitHub
  tools/auth → write the blocker brief and move to `Human Review` per
  rule 7.

## PR completion

Before the PR can merge it must carry the `symphony` label:
`GET /repos/<repo>/issues/<pr>/labels` to check,
`POST /repos/<repo>/issues/<pr>/labels` `{"labels": ["symphony"]}` to add
(create the repo label first with `POST /repos/<repo>/labels`
`{"name": "symphony"}` when missing). If the token lacks permission,
write a blocker brief and move to `Human Review` — do not silently skip.


## This isolated live acceptance run on host 214

This is a disposable, private Symphony test repository used only for acceptance on the 214 host. Work only on this test issue. Git uses HTTPS with the preconfigured gh credential helper and the 199 HTTP proxy (github.com:443 is only reachable through it on this host). Use github_api for metadata/comments/PRs/checks; do not run gh yourself or print credentials. Default branch is main. Run npm test and inspect the latest PR check-runs. Human Review is a deliberate stop; never self-approve Merging. When a Rework run deletes/recreates the workpad, read the human feedback first, clear stale pointers with task_api, use a unique new branch, and update all pointers.
