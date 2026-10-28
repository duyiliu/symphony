"""WORKFLOW.md loading: YAML front matter + Liquid prompt template body."""

import os
import re

try:
    import yaml
except ImportError:  # pragma: no cover - environment dependent
    yaml = None


def _build_yamerl_loader():
    """SafeLoader with yamerl's boolean vocabulary.

    PyYAML and yamerl both resolve YAML 1.1 booleans
    (``yes/no/true/false/on/off`` with case variants); yamerl additionally
    treats single-letter ``y``/``n`` as booleans, so that set is used here.
    """
    if yaml is None:
        return None

    class _YamerlLoader(yaml.SafeLoader):
        pass

    _YamerlLoader.yaml_implicit_resolvers = {
        key: [(tag, regexp) for tag, regexp in resolvers
              if tag != "tag:yaml.org,2002:bool"]
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    _YamerlLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(
            r"^(?:y|Y|yes|Yes|YES|n|N|no|No|NO"
            r"|true|True|TRUE|false|False|FALSE"
            r"|on|On|ON|off|Off|OFF)$"),
        list("yYnNtTfFoO"))
    return _YamerlLoader


_YAMERL_LOADER = _build_yamerl_loader()


class WorkflowError(Exception):
    def __init__(self, reason, detail=None):
        self.reason = reason
        self.detail = detail
        super().__init__(self.describe())

    def describe(self):
        if self.reason == "missing_workflow_file":
            return "Missing WORKFLOW.md at %s: %s" % (self.detail[0], self.detail[1])
        if self.reason == "workflow_parse_error":
            return "Failed to parse WORKFLOW.md: %s" % (self.detail,)
        if self.reason == "workflow_front_matter_not_a_map":
            return "Failed to parse WORKFLOW.md: workflow front matter must decode to a map"
        if self.reason == "invalid_workflow_config":
            return "Invalid WORKFLOW.md config: %s" % (self.detail,)
        return "%s: %s" % (self.reason, self.detail)


class Workflow:
    """Loaded ``WORKFLOW.md``: ``config`` (front matter map) + ``prompt_template``."""

    def __init__(self, config, prompt_template):
        self.config = config
        self.prompt_template = prompt_template


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError as error:
        raise WorkflowError("missing_workflow_file", (path, error.strerror or str(error)))
    return parse(content)


def parse(content):
    front_lines, prompt_lines = split_front_matter(content)
    front_matter = front_matter_to_map(front_lines)
    prompt = "\n".join(prompt_lines).strip()
    return Workflow(front_matter, prompt)


def split_front_matter(content):
    lines = content.splitlines()
    if lines and lines[0] == "---":
        front = []
        index = 1
        while index < len(lines) and lines[index] != "---":
            front.append(lines[index])
            index += 1
        if index < len(lines):
            return front, lines[index + 1:]
        return front, []
    return [], lines


def front_matter_to_map(lines):
    text = "\n".join(lines)
    if text.strip() == "":
        return {}
    if yaml is None:
        raise WorkflowError(
            "workflow_parse_error",
            "PyYAML is required to parse WORKFLOW.md front matter "
            "(pip3 install pyyaml)")
    try:
        decoded = yaml.load(text, Loader=_YAMERL_LOADER)
    except yaml.YAMLError as error:
        raise WorkflowError("workflow_parse_error", str(error))
    # ``~``/``null``/comment-only documents decode to None, which is not a
    # map — same ``workflow_front_matter_not_a_map`` error as Elixir.
    if not isinstance(decoded, dict):
        raise WorkflowError("workflow_front_matter_not_a_map")
    return decoded


def default_workflow_path():
    return os.path.join(os.getcwd(), "WORKFLOW.md")


DEFAULT_PROMPT_TEMPLATE = """\
You are working on an issue from the configured tracker.

Identifier: {{ issue.identifier }}
Title: {{ issue.title }}

Body:
{% if issue.description %}
{{ issue.description }}
{% else %}
No description provided.
{% endif %}
"""
