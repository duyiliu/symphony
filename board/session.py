#!/usr/bin/env python3
"""Browse the pi sessions Symphony records, grouped by issue.

    session issues                  list issues that have recorded runs
    session issue GH-18             list the runs of one issue
    session show GH-18              render the newest run of an issue
    session show GH-18@2            render its second run (oldest is 1)
    session show last               render the newest run overall
    session show 3                  render the third newest run overall
    session show <id-prefix>        render the first run matching an id
    session show <target> --full    do not clip long thinking or tool output
    session export [--out DIR]      write static HTML grouped by issue
"""
import glob
import html
import json
import os
import sys

SESS_DIR = os.environ.get("SYMPHONY_SESSIONS", "/root/symphony/sessions")
DEFAULT_OUT = os.path.join(SESS_DIR, "html")


def read_records(path):
    records = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def session_header(records):
    return next((r for r in records if r.get("type") == "session"), {})


def issue_of(header):
    cwd = (header.get("cwd") or "").rstrip("/")
    return os.path.basename(cwd) or "unknown"


def human_size(size):
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}G"


def all_runs():
    runs = []
    for path in glob.glob(os.path.join(SESS_DIR, "*.jsonl")):
        records = read_records(path)
        header = session_header(records)
        runs.append(
            {
                "path": path,
                "id": header.get("id") or "",
                "short_id": (header.get("id") or "")[:8],
                "issue": issue_of(header),
                "started": (header.get("timestamp") or "")[:19].replace("T", " "),
                "size": os.path.getsize(path),
                "messages": sum(1 for r in records if r.get("type") == "message"),
                "mtime": os.path.getmtime(path),
            }
        )
    runs.sort(key=lambda r: r["mtime"])
    return runs


def group_by_issue(runs):
    grouped = {}
    for run in runs:
        grouped.setdefault(run["issue"], []).append(run)
    for entries in grouped.values():
        entries.sort(key=lambda r: r["mtime"])
    return grouped


def clip(text, limit):
    text = text or ""
    if limit and len(text) > limit:
        return text[:limit].rstrip() + f"\n  ... (+{len(text) - limit} chars, use --full)"
    return text


def indent(text, prefix="  "):
    return prefix + text.replace("\n", "\n" + prefix)


def cmd_issues():
    runs = all_runs()
    if not runs:
        print(f"no sessions under {SESS_DIR}")
        return
    grouped = group_by_issue(runs)
    print(f"{'issue':<12} {'runs':>4} {'total':>7}  last run")
    for issue in sorted(grouped, key=lambda i: grouped[i][-1]["mtime"], reverse=True):
        entries = grouped[issue]
        total = sum(e["size"] for e in entries)
        print(f"{issue:<12} {len(entries):>4} {human_size(total):>7}  {entries[-1]['started']}")


def cmd_issue(name):
    runs = [r for r in all_runs() if r["issue"].lower() == name.lower()]
    if not runs:
        sys.exit(f"no recorded runs for issue {name!r}")
    print(f"{name}  {len(runs)} run(s)")
    for index, run in enumerate(runs, 1):
        print(
            f"  {index}. {run['started']}  {human_size(run['size']):>7} "
            f"{run['messages']:>4} msgs  {run['short_id']}  ({name}@{index})"
        )


def resolve(target, runs):
    if target == "last":
        return runs[-1] if runs else None
    if "@" in target:
        issue, _, ordinal = target.partition("@")
        entries = [r for r in runs if r["issue"].lower() == issue.lower()]
        try:
            return entries[int(ordinal) - 1]
        except (ValueError, IndexError):
            return None
    if target.isdigit():
        index = int(target)
        return runs[-index] if 0 < index <= len(runs) else None
    if os.path.exists(target):
        return next((r for r in runs if r["path"] == target), None)
    matches = [r for r in runs if r["id"].startswith(target)]
    if matches:
        return matches[0]
    entries = [r for r in runs if r["issue"].lower() == target.lower()]
    return entries[-1] if entries else None


def render_text(run, full=False):
    records = read_records(run["path"])
    text_limit = None if full else 1200
    think_limit = None if full else 400

    print("=" * 78)
    print(f"{run['issue']}  started {run['started']}  {run['short_id']}")
    print(run["path"])
    print("=" * 78)

    for record in records:
        if record.get("type") != "message":
            continue
        message = record.get("message", {})
        role = message.get("role")
        if role == "system":
            continue
        stamp = (record.get("timestamp") or "")[11:19]
        content = message.get("content")

        print(f"\n[{stamp}] {str(role).upper()}")
        if isinstance(content, str):
            print(indent(clip(content, text_limit)))
            continue

        for part in content or []:
            kind = part.get("type")
            if kind == "text":
                print(indent(clip(part.get("text", ""), text_limit)))
            elif kind == "thinking":
                print(indent("[think] " + clip(part.get("thinking", ""), think_limit)))
            elif kind == "toolCall":
                args = json.dumps(part.get("arguments", {}), ensure_ascii=False)
                print(indent(f"[tool] {part.get('name')} {clip(args, think_limit)}"))

        usage = message.get("usage") or {}
        if role == "assistant" and usage:
            print(f"  (tokens in={usage.get('input')} out={usage.get('output')})")


STYLE = """
body{background:#111418;color:#d6d9dd;font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0;padding:24px}
a{color:#7cb8ff;text-decoration:none} a:hover{text-decoration:underline}
h1{font-size:18px;margin:0 0 4px} h2{font-size:15px;margin:28px 0 8px;color:#9aa4b0}
.meta{color:#7c8794;font-size:12px;margin-bottom:20px}
table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #232830;font-size:13px}
th{color:#7c8794;font-weight:normal}
.msg{margin:14px 0 0;border-left:2px solid #2a313a;padding:2px 0 2px 12px}
.role{color:#8ab4f8;font-size:12px;margin-bottom:4px}
.user{border-left-color:#5f7d95} .assistant{border-left-color:#4f8a5f}
.toolresult{border-left-color:#5c5340} .toolresult pre{color:#8d8674}
pre{white-space:pre-wrap;word-break:break-word;margin:4px 0;font:inherit}
.think{color:#8d96a3} .tool{color:#d9b26a}
.tokens{color:#5f6874;font-size:12px;margin-top:4px}
"""


def esc(text):
    return html.escape(text or "")


def render_html(run, full=False):
    records = read_records(run["path"])
    text_limit = None if full else 4000
    think_limit = None if full else 1500
    blocks = []

    for record in records:
        if record.get("type") != "message":
            continue
        message = record.get("message", {})
        role = message.get("role")
        if role == "system":
            continue
        stamp = (record.get("timestamp") or "")[11:19]
        content = message.get("content")
        parts = []

        if isinstance(content, str):
            parts.append(f"<pre>{esc(content[:text_limit] if text_limit else content)}</pre>")
        else:
            for part in content or []:
                kind = part.get("type")
                if kind == "text":
                    body = part.get("text", "")
                    parts.append(f"<pre>{esc(body[:text_limit] if text_limit else body)}</pre>")
                elif kind == "thinking":
                    body = part.get("thinking", "")
                    parts.append(f"<pre class='think'>{esc(body[:think_limit] if think_limit else body)}</pre>")
                elif kind == "toolCall":
                    args = json.dumps(part.get("arguments", {}), ensure_ascii=False)
                    clipped = args[:think_limit] if think_limit else args
                    parts.append(f"<pre class='tool'>[tool] {esc(part.get('name'))} {esc(clipped)}</pre>")

        usage = message.get("usage") or {}
        footer = ""
        if role == "assistant" and usage:
            footer = f"<div class='tokens'>tokens in={usage.get('input')} out={usage.get('output')}</div>"

        blocks.append(
            f"<div class='msg {esc(str(role))}'><div class='role'>[{stamp}] {esc(str(role).upper())}</div>"
            f"{''.join(parts)}{footer}</div>"
        )

    return (
        f"<h1>{esc(run['issue'])} · {esc(run['started'])}</h1>"
        f"<div class='meta'>{esc(run['short_id'])} · {human_size(run['size'])} · "
        f"{run['messages']} messages · <a href='index.html'>back to {esc(run['issue'])}</a></div>"
        f"{''.join(blocks)}"
    )


def page(title, body):
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{esc(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )


def cmd_export(out_dir, full=False):
    runs = all_runs()
    if not runs:
        sys.exit("nothing to export")
    grouped = group_by_issue(runs)
    os.makedirs(out_dir, exist_ok=True)

    rows = []
    for issue in sorted(grouped, key=lambda i: grouped[i][-1]["mtime"], reverse=True):
        entries = grouped[issue]
        total = sum(e["size"] for e in entries)
        rows.append(
            f"<tr><td><a href='{esc(issue)}/index.html'>{esc(issue)}</a></td>"
            f"<td>{len(entries)}</td><td>{human_size(total)}</td>"
            f"<td>{esc(entries[-1]['started'])}</td></tr>"
        )

    index_body = (
        "<h1>Symphony runs</h1>"
        f"<div class='meta'>{len(runs)} sessions across {len(grouped)} issues · {esc(SESS_DIR)}</div>"
        "<table><tr><th>issue</th><th>runs</th><th>size</th><th>last run</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as handle:
        handle.write(page("Symphony runs", index_body))

    for issue, entries in grouped.items():
        issue_dir = os.path.join(out_dir, issue)
        os.makedirs(issue_dir, exist_ok=True)

        rows = []
        for position, run in enumerate(entries, 1):
            name = f"{run['short_id']}.html"
            rows.append(
                f"<tr><td>{position}</td><td><a href='{esc(name)}'>{esc(run['started'])}</a></td>"
                f"<td>{human_size(run['size'])}</td><td>{run['messages']}</td>"
                f"<td>{esc(run['short_id'])}</td></tr>"
            )
            with open(os.path.join(issue_dir, name), "w", encoding="utf-8") as handle:
                handle.write(page(f"{issue} {run['started']}", render_html(run, full)))

        body = (
            f"<h1>{esc(issue)}</h1>"
            f"<div class='meta'>{len(entries)} run(s) · <a href='../index.html'>all issues</a></div>"
            "<table><tr><th>#</th><th>started</th><th>size</th><th>msgs</th><th>session</th></tr>"
            + "".join(rows)
            + "</table>"
        )
        with open(os.path.join(issue_dir, "index.html"), "w", encoding="utf-8") as handle:
            handle.write(page(issue, body))

    print(f"exported {len(runs)} runs for {len(grouped)} issues to {out_dir}")
    print(f"open {os.path.join(out_dir, 'index.html')}")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    command = args[0]
    rest = [a for a in args[1:] if not a.startswith("--")]
    full = "--full" in args

    if command == "issues":
        return cmd_issues()
    if command == "issue":
        if not rest:
            sys.exit("usage: session issue <ISSUE>")
        return cmd_issue(rest[0])
    if command == "show":
        runs = all_runs()
        target = rest[0] if rest else "last"
        run = resolve(target, runs)
        if not run:
            sys.exit(f"no run matching {target!r}")
        return render_text(run, full)
    if command == "export":
        out_dir = DEFAULT_OUT
        if "--out" in args:
            out_dir = args[args.index("--out") + 1]
        return cmd_export(out_dir, full)

    print(__doc__)


if __name__ == "__main__":
    main()
