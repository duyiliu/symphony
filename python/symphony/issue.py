"""Normalised tracker work item used by the orchestrator."""

from dataclasses import dataclass, field

from .util import normalize_label


@dataclass
class Issue:
    id: str = None
    native_ref: dict = None
    identifier: str = None
    title: str = None
    description: str = None
    priority: int = None
    state: str = None
    branch_name: str = None
    url: str = None
    assignee_id: str = None
    blocked_by: list = field(default_factory=list)
    labels: list = field(default_factory=list)
    dispatchable: bool = False
    created_at: object = None
    updated_at: object = None

    def routable(self, required_labels):
        if self.dispatchable is not True \
                or not isinstance(self.labels, list) \
                or not isinstance(required_labels, list):
            return False
        issue_labels = {normalize_label(label) for label in self.labels}
        return all(
            normalize_label(required) in issue_labels
            for required in required_labels
        )

    def to_template_map(self):
        """Context for the Liquid prompt template (string keys, plain values)."""
        from .util import iso8601

        return {
            "id": self.id,
            "native_ref": self.native_ref,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "state": self.state,
            "branch_name": self.branch_name,
            "url": self.url,
            "assignee_id": self.assignee_id,
            "blocked_by": self.blocked_by or [],
            "labels": list(self.labels or []),
            "dispatchable": self.dispatchable,
            "created_at": iso8601(self.created_at) if self.created_at else None,
            "updated_at": iso8601(self.updated_at) if self.updated_at else None,
        }
