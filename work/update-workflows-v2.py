from pathlib import Path

paths = [Path('deploy/WORKFLOW.199.md'), Path(r'D:/workspace/playground/symphony-local/config/WORKFLOW.local.md')]
for path in paths:
    text = path.read_text(encoding='utf-8')
    start = text.index('# Auto merge')
    end = text.index('# Failure / handoff', start)
    text = text[:start] + '''# Delivery handoff (Python GitHub host v2)

- `agent` means AI work is queued or running. After accepting delivery the host persists ownership and removes `agent`; the independent host checker handles CI, risk, approval and merge without AI polling.
- After `deliver` succeeds, end the turn. Do not call `merge`, poll CI, sleep, or add/remove execution labels. If the tool returns `new_rework_feedback`, read new feedback and continue before delivering again. If handoff is uncertain, preserve the workspace and report the tool result; do not bypass authorization.
- A human may post feedback on the PR and add `agent` again (the board's Continue optimization action does both). That returns the previous delivery to development. Keep the existing open PR; adding `agent` is never merge approval.
- On every start/resume, locate the linked PR and read the Issue, Workpad, top-level PR comments, inline review comments/replies and review summaries. Before delivery, repeat this feedback sweep. Treat every actionable item as unfinished until addressed or explicitly replied to with a justified explanation. Update the Workpad with resolutions and exact validation evidence.
- Ordinary discussion comments do not themselves start an Agent. During a run, check new comments before delivery; do not start a parallel task. If the prior PR is closed or merged, inspect the Issue and use a fresh branch/PR only when further work is explicitly requested.
- For CI/conflict repair the host first withdraws the old delivery and queues `agent` again, with bounded repair attempts. Reuse work already completed, fix the cause, run relevant checks, and deliver again.
- `needs-human` pauses AI and automatic merge. Only a human resume may remove the pause and queue execution. Never resume or approve yourself.
- Human approval is a valid non-author Review for the delivered SHA or the explicitly configured approval label. The host invalidates old label approval on a new delivery. Do not claim an approval merely because a label is present.

''' + text[end:]
    path.write_text(text, encoding='utf-8')
    print(path)
