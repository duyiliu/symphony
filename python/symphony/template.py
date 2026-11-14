"""Minimal Liquid-subset renderer for the WORKFLOW.md prompt template.

Supported syntax (covers what Symphony workflow templates use):

- ``{{ path.to.value }}`` output with strict variable lookup
- ``{{ value | filter: arg }}`` with Liquid filters (strict: unknown filters fail)
- ``{% if expr %} {% elsif expr %} {% else %} {% endif %}``
- ``{% unless expr %} ... {% endunless %}``
- ``{% for item in list %} ... {% else %} ... {% endfor %}`` with ``forloop``
- ``{% assign name = expr %}``, ``{% capture name %}``, ``{% case %}``
- ``{% comment %} ... {% endcomment %}``
- ``{% raw %} ... {% endraw %}``
- comparisons ``==`` ``!=`` ``>`` ``<`` ``>=`` ``<=`` and ``contains``

Liquid truthiness applies: only ``None`` and ``False`` are falsy; empty
strings and empty lists are truthy. Arrays render by concatenation, matching
Liquid's array output.
"""

import datetime
import json
import re
import urllib.parse

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


class _Assign:
    __slots__ = ("name", "expr")

    def __init__(self, name, expr):
        self.name = name
        self.expr = expr


class _Capture:
    __slots__ = ("name", "body")

    def __init__(self, name, body):
        self.name = name
        self.body = body


class _Case:
    __slots__ = ("expr", "branches", "else_body")

    def __init__(self, expr, branches, else_body):
        self.expr = expr
        self.branches = branches
        self.else_body = else_body


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
                elif keyword == "assign":
                    nodes.append(self.parse_assign(rest))
                elif keyword == "capture":
                    nodes.append(self.parse_capture(rest))
                elif keyword == "case":
                    nodes.append(self.parse_case(rest))
                elif keyword == "raw":
                    nodes.append(self.parse_raw())
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

    def parse_assign(self, spec):
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$", spec, re.S)
        if not match:
            raise TemplateError("malformed assign tag: %s" % spec)
        return _Assign(match.group(1), match.group(2).strip())

    def parse_capture(self, spec):
        name = spec.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            raise TemplateError("malformed capture tag: %s" % spec)
        body = self.parse_body({"endcapture"})
        self.expect_tag("endcapture")
        return _Capture(name, body)

    def parse_case(self, expr):
        branches = []
        else_body = []
        while self.pos < len(self.tokens):
            if not (self.tokens[self.pos] or "").strip():
                self.pos += 1
                continue
            tag = self.peek_tag()
            if tag is None:
                raise TemplateError("unclosed case block")
            keyword, rest = tag
            if keyword == "when":
                self.pos += 1
                values = [
                    value.strip()
                    for value in re.split(r"\s+or\s+|,", rest)
                    if value.strip()
                ]
                branches.append(
                    (values, self.parse_body({"when", "else", "endcase"})))
            elif keyword == "else":
                self.pos += 1
                else_body = self.parse_body({"endcase"})
            elif keyword == "endcase":
                self.pos += 1
                return _Case(expr.strip(), branches, else_body)
            else:
                raise TemplateError("unexpected tag in case block: %s" % keyword)
        raise TemplateError("unclosed case block")

    def parse_raw(self):
        parts = []
        while self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            self.pos += 1
            tag = TAG_RE.match(token or "")
            if tag and tag.group(1).strip() == "endraw":
                return "".join(parts)
            parts.append(token)
        raise TemplateError("unclosed raw block")


def parse(source):
    parser = _Parser(source)
    return parser.parse_body(set())


def render(source, context):
    return render_nodes(parse(source), dict(context))


def render_nodes(nodes, context):
    out = []
    _render_nodes(nodes, context, out)
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
        elif isinstance(node, _Assign):
            context[node.name] = _eval_expr(node.expr, context)
        elif isinstance(node, _Capture):
            captured = []
            _render_nodes(node.body, context, captured)
            context[node.name] = "".join(captured)
        elif isinstance(node, _Case):
            value = _eval_expr(node.expr, context)
            rendered = False
            for values, body in node.branches:
                for raw in values:
                    if value == _eval_operand(raw, context):
                        _render_nodes(body, context, out)
                        rendered = True
                        break
                if rendered:
                    break
            if not rendered:
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
    segments = _split_top_level(expr, "|")
    value = _eval_value(segments[0].strip(), context)
    for segment in segments[1:]:
        name, _, arg_text = segment.partition(":")
        name = name.strip()
        if name not in FILTERS:
            raise TemplateError("unknown Liquid filter: %s" % name)
        if arg_text.strip():
            arguments = [
                _eval_operand(part, context)
                for part in _split_top_level(arg_text, ",")
            ]
        else:
            arguments = []
        value = FILTERS[name](value, *arguments)
    return value


def _eval_value(value, context):
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


def _split_top_level(text, separator):
    """Split on ``separator`` outside of single/double quoted strings."""
    parts, buffer, quote = [], [], None
    for character in text:
        if quote is not None:
            buffer.append(character)
            if character == quote:
                quote = None
        elif character in "\"'":
            quote = character
            buffer.append(character)
        elif character == separator:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(character)
    parts.append("".join(buffer))
    return parts


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


def _text(value):
    return _stringify(value)


def _number(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    text = _text(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return 0


def _compact_number(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _sequence(value):
    return list(value) if isinstance(value, (list, tuple)) else []


def _item_value(item, key):
    if isinstance(item, dict):
        return item.get(key)
    return None


def _filter_append(value, suffix=""):
    return _text(value) + _text(suffix)


def _filter_prepend(value, prefix=""):
    return _text(prefix) + _text(value)


def _filter_capitalize(value):
    text = _text(value)
    return text[:1].upper() + text[1:].lower() if text else ""


def _filter_downcase(value):
    return _text(value).lower()


def _filter_upcase(value):
    return _text(value).upper()


def _filter_strip(value):
    return _text(value).strip()


def _filter_lstrip(value):
    return _text(value).lstrip()


def _filter_rstrip(value):
    return _text(value).rstrip()


def _filter_strip_newlines(value):
    return _text(value).replace("\r", "").replace("\n", "")


def _filter_newline_to_br(value):
    return _text(value).replace("\r\n", "\n").replace("\n", "<br />\n")


def _filter_replace(value, old="", new=""):
    return _text(value).replace(_text(old), _text(new))


def _filter_replace_first(value, old="", new=""):
    return _text(value).replace(_text(old), _text(new), 1)


def _filter_replace_last(value, old="", new=""):
    text, needle, replacement = _text(value), _text(old), _text(new)
    if needle == "":
        return text
    index = text.rfind(needle)
    if index < 0:
        return text
    return text[:index] + replacement + text[index + len(needle):]


def _filter_remove(value, old=""):
    return _text(value).replace(_text(old), "")


def _filter_remove_first(value, old=""):
    return _text(value).replace(_text(old), "", 1)


def _filter_remove_last(value, old=""):
    return _filter_replace_last(value, old, "")


def _filter_truncate(value, length=50, ellipsis="..."):
    text, limit, suffix = _text(value), int(_number(length)), _text(ellipsis)
    if len(text) <= limit:
        return text
    keep = max(limit - len(suffix), 0)
    return text[:keep] + suffix


def _filter_truncatewords(value, count=15, ellipsis="..."):
    words = _text(value).split()
    limit = int(_number(count))
    if len(words) <= limit:
        return _text(value)
    return " ".join(words[:limit]) + _text(ellipsis)


def _filter_split(value, separator=" "):
    return _text(value).split(_text(separator))


def _filter_escape(value):
    return (
        _text(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


_ESCAPED_ENTITY_RE = re.compile(r"&(amp|lt|gt|quot|#39|#x27);")


def _filter_escape_once(value):
    placeholders = {}
    text = _text(value)

    def keep(match):
        key = "\x00%d\x00" % len(placeholders)
        placeholders[key] = match.group(0)
        return key

    text = _ESCAPED_ENTITY_RE.sub(keep, text)
    text = _filter_escape(text)
    for key, original in placeholders.items():
        text = text.replace(key, original)
    return text


def _filter_url_encode(value):
    return urllib.parse.quote_plus(_text(value))


def _filter_url_decode(value):
    return urllib.parse.unquote_plus(_text(value))


def _filter_default(value, fallback=""):
    if value is None or value is False:
        return fallback
    if isinstance(value, (str, list, tuple, dict)) and len(value) == 0:
        return fallback
    return value


def _filter_size(value):
    if isinstance(value, (str, list, tuple, dict)):
        return len(value)
    return 0


def _filter_slice(value, offset=0, length=1):
    start = int(_number(offset))
    count = int(_number(length))
    if isinstance(value, str):
        text = value
        if start < 0:
            start = max(len(text) + start, 0)
        return text[start:start + count]
    items = _sequence(value)
    if start < 0:
        start = max(len(items) + start, 0)
    return items[start:start + count]


def _filter_join(value, separator=" "):
    return _text(separator).join(_text(item) for item in _sequence(value))


def _filter_first(value):
    if isinstance(value, str):
        return value[:1]
    items = _sequence(value)
    return items[0] if items else None


def _filter_last(value):
    if isinstance(value, str):
        return value[-1:]
    items = _sequence(value)
    return items[-1] if items else None


def _filter_map(value, key):
    return [_item_value(item, key) for item in _sequence(value)]


def _filter_where(value, key, expected=None):
    return [
        item for item in _sequence(value)
        if (_item_value(item, key) if expected is None
            else _item_value(item, key) == expected)
    ]


def _filter_sort(value, key=None):
    items = _sequence(value)
    if key is None:
        return sorted(items, key=_sort_key)
    return sorted(
        items,
        key=lambda item: _sort_key(_item_value(item, key)),
    )


def _sort_key(item):
    if item is None:
        return (2, "")
    if isinstance(item, (int, float)) and not isinstance(item, bool):
        return (0, item)
    return (1, _text(item))


def _filter_sort_natural(value, key=None):
    items = _sequence(value)
    if key is None:
        return sorted(items, key=lambda item: _text(item).lower())
    return sorted(
        items,
        key=lambda item: _text(_item_value(item, key)).lower(),
    )


def _filter_uniq(value):
    seen, result = set(), []
    for item in _sequence(value):
        marker = json.dumps(item, sort_keys=True, default=str) \
            if isinstance(item, (dict, list)) else item
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _filter_compact(value):
    return [item for item in _sequence(value) if item is not None]


def _filter_reverse(value):
    if isinstance(value, str):
        return value[::-1]
    return list(reversed(_sequence(value)))


def _filter_concat(value, other):
    return _sequence(value) + _sequence(other)


def _filter_plus(value, other=0):
    return _compact_number(_number(value) + _number(other))


def _filter_minus(value, other=0):
    return _compact_number(_number(value) - _number(other))


def _filter_times(value, other=1):
    return _compact_number(_number(value) * _number(other))


def _filter_divided_by(value, other=1):
    divisor = _number(other)
    if divisor == 0:
        raise TemplateError("divided_by: division by zero")
    return _compact_number(_number(value) / divisor)


def _filter_modulo(value, other=1):
    divisor = _number(other)
    if divisor == 0:
        raise TemplateError("modulo: division by zero")
    return _compact_number(_number(value) % divisor)


def _filter_abs(value):
    return abs(_number(value))


def _filter_ceil(value):
    import math

    return math.ceil(_number(value))


def _filter_floor(value):
    import math

    return math.floor(_number(value))


def _filter_round(value, digits=0):
    return _compact_number(round(_number(value), int(_number(digits))))


def _filter_at_least(value, minimum=0):
    return max(_number(value), _number(minimum))


def _filter_at_most(value, maximum=0):
    return min(_number(value), _number(maximum))


def _parse_datetime(value):
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    text = _text(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _filter_date(value, fmt="%Y-%m-%d"):
    parsed = _parse_datetime(value)
    if parsed is None:
        return _text(value)
    return parsed.strftime(_text(fmt))


def _filter_json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


FILTERS = {
    "append": _filter_append,
    "prepend": _filter_prepend,
    "capitalize": _filter_capitalize,
    "downcase": _filter_downcase,
    "upcase": _filter_upcase,
    "strip": _filter_strip,
    "lstrip": _filter_lstrip,
    "rstrip": _filter_rstrip,
    "strip_newlines": _filter_strip_newlines,
    "newline_to_br": _filter_newline_to_br,
    "replace": _filter_replace,
    "replace_first": _filter_replace_first,
    "replace_last": _filter_replace_last,
    "remove": _filter_remove,
    "remove_first": _filter_remove_first,
    "remove_last": _filter_remove_last,
    "truncate": _filter_truncate,
    "truncatewords": _filter_truncatewords,
    "split": _filter_split,
    "escape": _filter_escape,
    "escape_once": _filter_escape_once,
    "url_encode": _filter_url_encode,
    "url_decode": _filter_url_decode,
    "default": _filter_default,
    "size": _filter_size,
    "slice": _filter_slice,
    "join": _filter_join,
    "first": _filter_first,
    "last": _filter_last,
    "map": _filter_map,
    "where": _filter_where,
    "sort": _filter_sort,
    "sort_natural": _filter_sort_natural,
    "uniq": _filter_uniq,
    "compact": _filter_compact,
    "reverse": _filter_reverse,
    "concat": _filter_concat,
    "plus": _filter_plus,
    "minus": _filter_minus,
    "times": _filter_times,
    "divided_by": _filter_divided_by,
    "modulo": _filter_modulo,
    "abs": _filter_abs,
    "ceil": _filter_ceil,
    "floor": _filter_floor,
    "round": _filter_round,
    "at_least": _filter_at_least,
    "at_most": _filter_at_most,
    "date": _filter_date,
    "json": _filter_json,
}
