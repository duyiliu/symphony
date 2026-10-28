"""GitHub REST/GraphQL helpers shared by the host workflow state machine.

Every failure raises :class:`GateError` — the caller converts it into a pause,
a retry or a tool error payload, mirroring ``throw({:workflow_error, _})``.
"""

import logging

from . import client as gh_client
from . import workflow_policy

log = logging.getLogger("symphony.github.workflow")


class GateError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("workflow_error: %r" % (reason,))


def ensure(condition, reason):
    # Elixir ``if`` truthiness: only None/False fail the gate (0/""/[] pass).
    if condition is None or condition is False:
        raise GateError(reason)


def fail(reason):
    raise GateError(reason)


def root(settings):
    return "/repos/%s" % workflow_policy.repo(settings)


def request(settings, method, path, body=None, params=None, github_client=None):
    client = github_client or gh_client.request
    try:
        result = client(method, path, params or {}, body, settings)
    except Exception as error:
        if isinstance(error, GateError):
            raise
        raise GateError(("github_request_failed", getattr(error, "reason", str(error))))
    status = result["status"]
    if 200 <= status <= 299:
        return result["body"]
    if status == 404 and method == "DELETE":
        return {}
    log.error(
        "GitHub API error status=%s method=%s path=%s body=%r",
        status, method, path, _safe_body(result["body"]))
    raise GateError(("github_status", status, method, path))


def _safe_body(body):
    text = repr(body)
    return text[:2000]


def _get_in(data, *keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def issue(settings, number, **kw):
    return request(settings, "GET", "%s/issues/%s" % (root(settings), number), None, **kw)


def pr(settings, number, **kw):
    return request(settings, "GET", "%s/pulls/%s" % (root(settings), number), None, **kw)


def events(settings, number, **kw):
    return pages(settings, "%s/issues/%s/events" % (root(settings), number), None, **kw)


def pages(settings, path, field, github_client=None, _page=1, _acc=None,
          params=None):
    acc = _acc or []
    if _page > 100:
        raise GateError("github_pagination_limit")
    merged = dict(params or {})
    merged.update({"per_page": 100, "page": _page})
    result = request(settings, "GET", path, None, params=merged,
                     github_client=github_client)
    rows = result.get(field) if field else result
    ensure(isinstance(rows, list), "invalid_github_page")
    if len(rows) < 100:
        return acc + rows
    return pages(settings, path, field, github_client, _page + 1, acc + rows,
                 params)


def linked_prs(settings, number, **kw):
    owner, repo_name = workflow_policy.repo(settings).split("/", 1)
    query = """
    query($owner: String!, $repo: String!, $issue: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $issue) {
          closedByPullRequestsReferences(first: 100, includeClosedPrs: true) {
            nodes { number state }
            pageInfo { hasNextPage }
          }
        }
      }
    }
    """
    result = request(
        settings, "POST", "/graphql",
        {"query": query, "variables": {"owner": owner, "repo": repo_name, "issue": number}},
        **kw)
    ensure_graphql_ok(result)
    connection = _get_in(
        result, "data", "repository", "issue",
        "closedByPullRequestsReferences")
    ensure(
        isinstance(connection, dict)
        and _get_in(connection, "pageInfo", "hasNextPage") is False,
        "ambiguous_pr_association",
    )
    return connection.get("nodes") or []


def merge_gate(settings, number, **kw):
    owner, repo_name = workflow_policy.repo(settings).split("/", 1)
    query = """
    query($owner: String!, $repo: String!, $pr: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $pr) { headRefOid baseRefName mergeStateStatus reviewDecision }
      }
    }
    """
    result = request(
        settings, "POST", "/graphql",
        {"query": query, "variables": {"owner": owner, "repo": repo_name, "pr": number}},
        **kw)
    ensure_graphql_ok(result)
    gate = _get_in(result, "data", "repository", "pullRequest")
    ensure(isinstance(gate, dict), "missing_merge_gate")
    return gate


def remove_label(settings, number, label, **kw):
    return request(
        settings, "DELETE",
        "%s/issues/%s/labels/%s" % (root(settings), number, label), None, **kw)


def add_label(settings, number, label, **kw):
    return request(
        settings, "POST", "%s/issues/%s/labels" % (root(settings), number),
        {"labels": [label]}, **kw)


def pr_files(settings, number, **kw):
    return pages(settings, "%s/pulls/%s/files" % (root(settings), number), None, **kw)


def pr_reviews(settings, number, **kw):
    return pages(settings, "%s/pulls/%s/reviews" % (root(settings), number), None, **kw)


def comment(settings, number, text, **kw):
    return request(
        settings, "POST", "%s/issues/%s/comments" % (root(settings), number),
        {"body": "[agent] " + text}, **kw)


def labels(issue):
    return [label["name"].lower() for label in (issue.get("labels") or [])]


def ensure_graphql_ok(result):
    if isinstance(result, dict) and "errors" in result:
        log.error("GitHub GraphQL error detail=%r", result["errors"])
        raise GateError("github_graphql_error")
