"""Explicit PR feedback requests; GitHub comments are the durable queue."""
import hashlib
import json

from .workflow_api import pages, root

TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}


def snapshot(settings, issue_number, pr_number):
    prefix = "[board] 继续优化（关联 GH-%s）" % issue_number
    comments = pages(settings, "%s/issues/%s/comments" % (root(settings), pr_number), None)
    selected = sorted(({
        "id": item["id"], "body": item.get("body") or "",
        "updated_at": item.get("updated_at") or item.get("created_at") or "",
        "url": item.get("html_url") or "",
    } for item in comments if isinstance(item.get("id"), int)
        and item.get("author_association") in TRUSTED
        and (item.get("body") or "").startswith(prefix)), key=lambda item: item["id"])
    digest = hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=False).encode()).hexdigest() if selected else ""
    return {"pr": pr_number, "digest": digest, "comments": selected}


def pending(delivery, feedback):
    return delivery.get("feedback_digest", "") != feedback["digest"]
