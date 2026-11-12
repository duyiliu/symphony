---
name: land
description:
  Land a PR by monitoring conflicts, resolving them, waiting for checks, and
  squash-merging when green; use when asked to land, merge, or shepherd a PR to
  completion.
---

# Land

Ported for the local board tracker: every `gh` call is replaced by a
`github_api` tool call (the shell carries no GitHub token; `path` is
relative to `https://api.github.com`, e.g. `/repos/<owner>/<repo>/pulls/3`);
`git` itself (fetch/merge/push) still runs in the shell over SSH. There is
no watcher script — you poll inside the session with `sleep` between
checks. The repo's default branch is `master` (`origin/master`).

## Goals

- Ensure the PR is conflict-free with master.
- Keep CI green and fix failures when they occur.
- Squash-merge the PR once checks pass.
- Do not yield until the PR is merged; keep the poll loop running unless
  the task state left `Merging` or you are blocked.
- No need to delete remote branches after merge; the repo auto-deletes head
  branches.

## Preconditions

- The task's `state` is `Merging` (re-read via `task_api get` — see the
  state guard below).
- `pr_number`/`branch_name` are recorded on the task. If absent, list open
  PRs with `github_api GET /repos/<repo>/pulls` and match `head.ref` to your
  branch. If none exists, create it (`POST /repos/<repo>/pulls`), then
  record `pr_number`/`pr_url`/`branch_name` on the task.
- You are on the PR branch with a clean working tree.

## State guard (required)

Before EVERY wait iteration and IMMEDIATELY before the merge request,
re-read the task with `task_api get`. If `state` is no longer `Merging`,
stop the land flow and follow the new state instead (Rework → full reset,
Human Review → end turn, terminal → end turn). A GitHub request already in
flight cannot be recalled — that residual window is an accepted baseline,
but you must never *start* a merge without a fresh `Merging` read.

## Steps

1. Locate the PR for the current branch (`branch_name`/`pr_number` on the
   task; else match `GET /repos/<repo>/pulls` by `head.ref`).
2. Confirm the full local validation gauntlet is green before any push.
3. If the working tree has uncommitted changes, commit and push before
   proceeding.
4. Check mergeability: `github_api GET /repos/<repo>/pulls/<n>` →
   `mergeable` / `mergeable_state`. These are REST values:
   `mergeable` is `true`/`false`/`null` (null = GitHub is still
   computing it) and `mergeable_state` is `clean`/`unstable`/`dirty`/
   `blocked`/`behind`/`unknown`, never the GraphQL `CONFLICTING`.
5. If conflicts exist (`mergeable` is `false` and `mergeable_state` is
   `dirty`, or merge produces conflict markers), `git fetch origin
   master && git merge origin/master`, resolve the conflicts, commit,
   `git push --force-with-lease` if history was rewritten — plain
   `git push` otherwise. If `mergeable` is `null` or `mergeable_state`
   is `unknown`, wait and re-fetch the PR; do not treat not-yet-computed
   as conflicted.
6. Ensure review comments (if present) are acknowledged and required fixes
   handled before merging.
7. Watch checks until complete (poll loop below).
8. If checks fail, pull the failing logs, fix locally, commit, push, and
   re-run the watch.
9. When all checks are green and review feedback is addressed, squash-merge
   with the PR title/body as the merge subject/body, then verify the merged
   result and write `task_api update state="Done"`.
10. **Context guard:** before implementing review feedback, confirm it does
    not conflict with the task's stated intent. If it conflicts, reply
    inline with a justification and escalate to `Human Review` with a
    blocker comment instead of changing code.
11. **Pushback template:** when disagreeing, reply inline with: acknowledge
    + rationale + offer alternative.
12. **Ambiguity gate:** when ambiguity blocks progress, post a blocker
    comment on the issue and move the task to `Human Review`. Do not
    implement until the human resolves it.
    - If you are confident you know better than the reviewer, you may
      proceed without asking, but reply inline with your rationale.
13. **Per-comment mode:** for each review comment, choose one of: accept,
    clarify, or push back. Reply inline stating the mode before changing
    code.
14. **Reply before change:** always respond with intended action before
    pushing code changes.

## Commands (github_api equivalents)

```
# Ensure branch and PR context
branch=$(git branch --show-current)
# github_api GET /repos/<repo>/pulls/<n>
#   -> .number .title .body .mergeable .mergeable_state .head.sha .state

# If the task already has pr_head_sha, compare it to .head.sha:
# a moved head means someone (or CI autofix) pushed — pull and re-verify.

# Poll loop — repeat until checks are complete:
#   github_api GET /repos/<repo>/commits/<head_sha>/check-runs
#     -> .check_runs[].status / .conclusion
#   github_api GET /repos/<repo>/commits/<head_sha>/status
#     -> .statuses[].state
# Any status "pending"/"queued"/"in_progress" (or null conclusion)
#   -> sleep 30, re-read the task (state guard), poll again.
# Any failure conclusion -> inspect the failing job:
#   github_api GET /repos/<repo>/actions/jobs/<job_id>/logs
#   (if the token cannot fetch logs, say so in the workpad plainly),
#   fix, commit, push, restart the loop.

# Squash-merge — only after a fresh `task_api get` shows `Merging`:
# github_api PUT /repos/<repo>/pulls/<n>/merge
#   {"sha": "<head sha you verified>", "merge_method": "squash",
#    "commit_title": "<pr title>", "commit_message": "<pr body>"}
# A 409/405 response means the head moved or merge was rejected — re-read
# the PR and the task, then re-decide; do not retry blindly.
# Confirm success: response body `merged: true`, or a follow-up
# GET /repos/<repo>/pulls/<n> shows `merged_at` set. Only then
# task_api update state="Done" (fresh expected_revision).
```

## Failure handling

- If checks fail, pull details via the check-runs/statuses and job-log
  endpoints above, fix locally, commit, push, and re-run the watch.
- Use judgment for flaky failures (e.g., a timeout on one platform) —
  proceeding is acceptable when you say why.
- If CI pushes an auto-fix commit (detected via a moved `.head.sha`), pull
  locally, merge `origin/master` if needed, add a real author commit, and
  force-push to retrigger CI, then restart the checks loop.
- If `mergeable` is `null` or `mergeable_state` is `unknown`, wait and
  re-fetch the PR — GitHub computes mergeability asynchronously.
- Do not merge while outstanding review comments (human or automated) are
  unanswered.
- Do not enable auto-merge.
- If the remote PR branch advanced due to your own prior force-push or
  merge, avoid redundant merges; `git push --force-with-lease` when
  rewriting.
- If the merge PUT returns success but the task update to `Done` hits
  `revision_conflict`, re-read the task: the PR is already merged — if a
  human moved the task (e.g., `Rework`), treat the PR as closed/merged
  context for the new state and proceed with the Rework flow (do NOT try
  to unmerge).

## Review handling

- Review feedback arrives as PR review comments and issue comments; treat
  them as blocking before merge.
- Fetch via `github_api`:
  - Inline review comments: `GET /repos/<repo>/pulls/<pr>/comments`
  - Review verdicts: `GET /repos/<repo>/pulls/<pr>/reviews`
  - PR conversation comments: `GET /repos/<repo>/issues/<pr>/comments`
- Reply to a specific review comment:
  `POST /repos/<repo>/pulls/<pr>/comments` with
  `{"body": "[codex] …", "in_reply_to": <numeric comment id>}`.
- `in_reply_to` must be the numeric review comment id, and the endpoint
  must include the PR number.
- All comments generated by you must be prefixed with `[codex]`.
- If feedback requires changes: reply with intended fixes, implement,
  commit, push, then reply with the fix details and commit sha in the same
  place.
- After a batch of fixes prefer one consolidated "review addressed"
  root-level comment on the PR (`POST /repos/<repo>/issues/<pr>/comments`).

## Scope + PR metadata

- The PR title and description should reflect the full scope of the
  change, not just the most recent fix (`PATCH /repos/<repo>/pulls/<n>`
  with `{"title","body"}` when stale).
- Classify each review comment as correctness, design, style,
  clarification, or scope; correctness concerns need concrete validation
  before closing.
- The PR must carry the `symphony` label before merge
  (`POST /repos/<repo>/issues/<pr>/labels` `{"labels":["symphony"]}`;
  create the repo label via `POST /repos/<repo>/labels` when missing).
