"""Minimal Liquid-subset renderer for the WORKFLOW.md prompt template.

Supported syntax (covers what Symphony workflow templates use):

- ``{{ path.to.value }}`` output with strict variable lookup
- ``{% if expr %} {% elsif expr %} {% else %} {% endif %}``
- ``{% unless expr %} ... {% endunless %}``
- ``{% for item in list %} ... {% else %} ... {% endfor %}`` with ``forloop``
- ``{% comment %} ... {% endcomment %}``
- comparisons ``==`` ``!=`` ``>`` ``<`` ``>=`` ``<=`` and ``contains``

Liquid truthiness applies: only ``None`` and ``False`` are falsy; empty
strings and empty lists are truthy. Arrays render by concatenation, matching
Liquid's array output.
"""

import re

TOKEN_RE = re.compile(r"({{.*?}}|{%.*?%})", re.S)
OUTPUT_RE = re.compile(r"^{{\s*(.*?)\s*}}$", re.S)
TAG_RE = re.compile(r"^{%\s*(.*?)\s*%}$", re.S)


class TemplateError(Exception):
    pass


class _Output:
    __slots__ = ("expr",)

    def __init__(self, expr):
        self.expr = expr


class _If:
    __slots__ = ("arms", "else_body")

    def __init__(self):
        self.arms = []  # [(condition_expr, body_nodes)]
        self.else_body = []


class _For:
    __slots__ = ("var", "iter_expr", "body", "else_body")

    def __init__(self, var, iter_expr):
        self.var = var
        self.iter_expr = iter_expr
        self.body = []
        self.else_body = []


class _Parser:
    def __init__(self, source):
        self.tokens = TOKEN_RE.split(source)
        self.pos = 0

    def peek_tag(self):
        """Return the tag keyword+rest at the cursor, or None for text/output."""
        if self.pos >= len(self.tokens):
            return None
        match = TAG_RE.match(self.tokens[self.pos] or "")
        if not match:
            return None
        words = match.group(1).strip()
        keyword, _, rest = words.partition(" ")
        return keyword, rest.strip()

    def parse_body(self, stop_keywords):
        """Parse nodes until a tag whose keyword is in ``stop_keywords``.

        Leaves the cursor ON the stopping tag. Raises on unexpected EOF.
        """
        nodes = []
        while self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            self.pos += 1
            if not token:
                continue
            tag = TAG_RE.match(token)
            if tag:
                words = tag.group(1).strip()
                keyword, _, rest = words.partition(" ")
                if keyword in stop_keywords:
                    self.pos -= 1
                    return nodes
                if keyword == "if":
                    nodes.append(self.parse_if(rest))
                elif keyword == "unless":
                    nodes.append(self.parse_unless(rest))
                elif keyword == "for":
                    nodes.append(self.parse_for(rest))
                elif keyword == "comment":
                    self.skip_tag("endcomment")
                else:
                    raise TemplateError("unsupported Liquid tag: %s" % words)
                continue
            output = OUTPUT_RE.match(token)
            if output:
                nodes.append(_Output(output.group(1)))
            else:
                nodes.append(token)
        if stop_keywords:
            raise TemplateError(
                "unclosed block, expected one of: %s" % ", ".join(sorted(stop_keywords))
            )
        return nodes

    def expect_tag(self, expected):
        tag = self.peek_tag()
        if tag and tag[0] == expected:
            self.pos += 1
            return
        raise TemplateError("expected {%% %s %%}, got %r" % (expected, self.current()))

    def current(self):
        if self.pos < len(self.tokens):
            return (self.tokens[self.pos] or "")[:40]
        return "<eof>"

    def skip_tag(self, end_keyword):
        while self.pos < len(self.tokens):
            tag = self.peek_tag()
            self.pos += 1
            if tag and tag[0] == end_keyword:
                return
        raise TemplateError("unclosed block, expected %s" % end_keyword)

    def parse_if(self, first_condition):
        node = _If()
        node.arms.append((first_condition.strip(), self.parse_body({"elsif", "else", "endif"})))
        while True:
            tag = self.peek_tag()
            if tag is None:
                raise TemplateError("unclosed if block")
            keyword, rest = tag
            if keyword == "elsif":
                self.pos += 1
                node.arms.append((rest, self.parse_body({"elsif", "else", "endif"})))
            elif keyword == "else":
                self.pos += 1
                node.else_body = self.parse_body({"endif"})
            else:
                break
        self.expect_tag("endif")
        return node

    def parse_unless(self, condition):
        node = _If()
        node.arms.append(("!(" + condition.strip() + ")",
                          self.parse_body({"else", "endunless"})))
        tag = self.peek_tag()
        if tag and tag[0] == "else":
            self.pos += 1
            node.else_body = self.parse_body({"endunless"})
        self.expect_tag("endunless")
        return node

    def parse_for(self, spec):
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+)$", spec)
        if not match:
            raise TemplateError("malformed for tag: %s" % spec)
        node = _For(match.group(1), match.group(2).strip())
        node.body = self.parse_body({"else", "endfor"})
        tag = self.peek_tag()
        if tag and tag[0] == "else":
            self.pos += 1
            node.else_body = self.parse_body({"endfor"})
        self.expect_tag("endfor")
        return node


def parse(source):
    parser = _Parser(source)
    return parser.parse_body(set())


def render(source, context):
    out = []
    _render_nodes(parse(source), context, out)
    return "".join(out)


def _truthy(value):
    return value is not None and value is not False


def _render_nodes(nodes, context, out):
    for node in nodes:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, _Output):
            out.append(_stringify(_eval_expr(node.expr, context)))
        elif isinstance(node, _If):
            for condition, body in node.arms:
                if _eval_condition(condition, context):
                    _render_nodes(body, context, out)
                    break
            else:
                _render_nodes(node.else_body, context, out)
        elif isinstance(node, _For):
            items = _eval_expr(node.iter_expr, context)
            if not isinstance(items, (list, tuple)):
                items = []
            if items:
                size = len(items)
                for idx, item in enumerate(items):
                    sub = dict(context)
                    sub[node.var] = item
                    sub["forloop"] = {
                        "index": idx + 1,
                        "index0": idx,
                        "first": idx == 0,
                        "last": idx == size - 1,
                        "length": size,
                    }
                    _render_nodes(node.body, sub, out)
            else:
                _render_nodes(node.else_body, context, out)
        else:
            raise TemplateError("unknown template node: %r" % (node,))


def _eval_condition(expr, context):
    expr = expr.strip()
    if expr.startswith("!(") and expr.endswith(")"):
        return not _eval_condition(expr[2:-1], context)
    if " contains " in expr:
        left, right = expr.split(" contains ", 1)
        haystack = _eval_expr(left.strip(), context)
        needle = _eval_operand(right.strip(), context)
        if isinstance(haystack, str):
            return str(needle) in haystack
        if isinstance(haystack, (list, tuple)):
            return needle in haystack
        return False
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        left, sep, right = expr.partition(op)
        if sep:
            return _compare(
                _eval_expr(left.strip(), context), op,
                _eval_operand(right.strip(), context))
    return _truthy(_eval_expr(expr, context))


def _compare(left, op, right):
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    try:
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
    except TypeError:
        return False
    return False


def _eval_operand(expr, context=None):
    expr = expr.strip()
    if len(expr) >= 2 and expr[0] == expr[-1] and expr[0] in "\"'":
        return expr[1:-1]
    if expr == "true":
        return True
    if expr == "false":
        return False
    if expr in ("nil", "null"):
        return None
    try:
        return int(expr)
    except ValueError:
        pass
    try:
        return float(expr)
    except ValueError:
        pass
    if context is not None and "." in expr:
        try:
            return _eval_expr(expr, context)
        except TemplateError:
            return expr
    if context is not None and expr in context:
        return context[expr]
    return expr


def _eval_expr(expr, context):
    expr = expr.strip()
    value, _, tail = expr.partition("|")
    if tail.strip():
        raise TemplateError("Liquid filters are not supported: %s" % expr)
    value = value.strip()
    if value in ("true", "false"):
        return value == "true"
    if value in ("nil", "null"):
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    parts = value.split(".")
    if parts[0] not in context:
        raise TemplateError("undefined variable: %s" % parts[0])
    current = context[parts[0]]
    for part in parts[1:]:
        current = _lookup(current, part, value)
    return current


def _lookup(container, key, expr):
    if container is None:
        raise TemplateError("nil has no member %s in %s" % (key, expr))
    if isinstance(container, dict):
        if key in container:
            return container[key]
        raise TemplateError("undefined key %s in %s" % (key, expr))
    if isinstance(container, (list, tuple)):
        if key == "size":
            return len(container)
        if key == "first":
            return container[0] if container else None
        if key == "last":
            return container[-1] if container else None
        if key.isdigit() and int(key) < len(container):
            return container[int(key)]
        raise TemplateError("invalid index %s in %s" % (key, expr))
    if hasattr(container, key):
        return getattr(container, key)
    raise TemplateError("undefined member %s in %s" % (key, expr))


def _stringify(value):
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (list, tuple)):
        return "".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return str(value)
    return str(value)
