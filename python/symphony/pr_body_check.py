"""Validate a PR body markdown file against the repository PR template.

Port of upstream ``mix pr_body.check``. Checks that the file contains every
``##``/``###`` heading the template declares and that required checkboxes are
filled in.

Usage:
    python -m symphony.pr_body_check --file path/to/body.md
"""

import argparse
import os
import re
import sys

_TEMPLATE_CANDIDATES = (
    ".github/pull_request_template.md",
    os.path.join(os.pardir, ".github", "pull_request_template.md"),
)

_HEADING = re.compile(r"^#{2,3}\s+.+")
_CHECKBOX_UNSET = re.compile(r"^\s*-\s*\[\s*\]\s*\S")
_CHECKBOX_SET = re.compile(r"^\s*-\s*\[[xX]\]\s*\S")


def _find_template(cwd=None):
    root = cwd or os.getcwd()
    for rel in _TEMPLATE_CANDIDATES:
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            return path
    return None


def _template_headings(text):
    return [line.strip() for line in text.splitlines()
            if _HEADING.match(line.strip())]


def _required_checkboxes(text):
    return [line.strip() for line in text.splitlines()
            if _CHECKBOX_UNSET.match(line)]


def check(template_text, body_text):
    """Return list of problems; empty means OK."""
    problems = []
    headings = _template_headings(template_text)
    body_lines = [line.strip() for line in body_text.splitlines()]
    for heading in headings:
        if heading not in body_lines:
            problems.append("missing heading: %s" % heading)
    required = _required_checkboxes(template_text)
    for checkbox in required:
        label = checkbox[len("- [ ]"):].strip() if checkbox.startswith("- [ ]") \
            else checkbox
        label = re.sub(r"^-\s*\[\s*\]\s*", "", checkbox).strip()
        matched = False
        for line in body_lines:
            if _CHECKBOX_SET.match(line) and label and label in re.sub(
                    r"^-\s*\[[xX]\]\s*", "", line):
                matched = True
                break
        if not matched:
            problems.append("checkbox not ticked: %s" % label)
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m symphony.pr_body_check",
        description="Validate a PR body against the repo PR template.")
    parser.add_argument("--file", required=True, help="Markdown body file")
    parser.add_argument("--template", default=None,
                        help="Override template path")
    args = parser.parse_args(argv)

    template_path = args.template or _find_template()
    if template_path is None or not os.path.isfile(template_path):
        sys.stderr.write("PR template not found\n")
        return 2
    if not os.path.isfile(args.file):
        sys.stderr.write("Body file not found: %s\n" % args.file)
        return 2

    with open(template_path, "r", encoding="utf-8") as handle:
        template = handle.read()
    with open(args.file, "r", encoding="utf-8") as handle:
        body = handle.read()

    problems = check(template, body)
    if problems:
        for problem in problems:
            sys.stderr.write("FAIL %s\n" % problem)
        return 1
    sys.stdout.write("PR body format OK\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
