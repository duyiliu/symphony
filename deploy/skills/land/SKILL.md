---
name: land
description:
  Land a PR by monitoring conflicts, resolving them, waiting for checks, and
  squash-merging when green; use when asked to land, merge, or shepherd a PR to
  completion.
---

# Land

## Host overrides (Symphony pi runtime)

- There is no `gh` CLI and no GitHub token in this process. Every GitHub call goes
  through the `github_api` tool (host-side auth):
  `github_api(method="GET", path="/repos/<owner>/<repo>/pulls/<n>")`.
- `github_api` accepts GET/POST/PATCH/PUT/DELETE with an optional JSON `body`.
- Merge is a **squash merge**:
  `PUT /repos/<owner>/<repo>/pulls/<n>/merge` with
  `{"sha": "<head sha>", "merge_method": "squash"}`.
- **Authorization binding**: when this run was authorized by a
  `symphony-merge-authorize` comment, you may merge only the SHA named there.
  If a repair changes the head, stop before merging (see "Authorized merges").

## Goals

- Ensure the PR is conflict-free with the base branch.
- Keep CI green and fix failures when they occur.
- Squash-merge the PR once checks pass.
- Do not yield to the user until the PR is merged; keep the watcher loop running
  unless blocked.
- No need to delete remote branches after merge; the repo auto-deletes head branches.

## Steps

1. Locate the PR for the issue: `GET /repos/<owner>/<repo>/pulls?state=open` and
   match the head branch `agent/issue-<id>`.
2. Confirm the full gauntlet is green locally before any push.
3. If the working tree has uncommitted changes, commit and push before proceeding.
4. Check mergeability: `GET /pulls/<n>` → `mergeable` / `mergeable_state`.
   `CONFLICTING` → fetch `origin/<base>`, merge, resolve conflicts, commit, push.
5. Watch checks: `GET /commits/<sha>/check-runs` and
   `GET /commits/<sha>/statuses`. Every reported check must be completed and
   successful. An empty rollup is NOT a pass.
6. If checks fail: read the failing check's `output` / `details_url`, fix locally,
   commit, push, and re-run checks.
7. Review feedback: `GET /pulls/<n>/reviews`, `GET /pulls/<n>/comments` and
   `GET /issues/<n>/comments`. `CHANGES_REQUESTED` or unanswered actionable human
   comments block the merge.
8. When green + conflict-free + reviews addressed, squash-merge:
   `PUT /pulls/<n>/merge` with `{"sha": "<head sha>", "merge_method": "squash"}`.
   Never admin, never force, never a different SHA.
9. Verify `merged: true`; then close the issue, remove `agent`, verify removal.

## Authorized merges (symphony-merge-authorize)

- Parse the authorization comment for the PR number and the authorized SHA.
- Re-read the PR before merging. If the current head differs from the authorized
  SHA, stop: comment the mismatch, remove `agent`, verify, stop.
- If a repair is needed (conflict or red CI) **and it changes the head**: push the
  repair, then STOP. Post one comment with what was fixed, the new head SHA, and
  that a new authorization is required. Remove `agent`, verify, stop.
  Do not merge the new head in this run.
- Merge only when the head still equals the authorized SHA and steps 4-7 are clean.

## Failure handling

- Do not merge while review comments (human or bot) are outstanding.
- If `mergeable` is `UNKNOWN`, wait and re-check.
- Never enable auto-merge.
- Record every blocker with the exact check name/state or review author in one
  issue comment; do not spam duplicates.

## Review handling

- Human review comments are blocking: reply with the intended action before
  pushing code changes.
- All agent-authored GitHub comments must be prefixed with `[agent]`.
