"""Liquid filters and tags used by Solid-compatible workflow templates."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from symphony import template  # noqa: E402
from symphony.template import TemplateError, render  # noqa: E402


class StringFilterTest(unittest.TestCase):
    def test_case_and_whitespace_filters(self):
        self.assertEqual(render("{{ v | upcase }}", {"v": "ab"}), "AB")
        self.assertEqual(render("{{ v | downcase }}", {"v": "AB"}), "ab")
        self.assertEqual(
            render("{{ v | capitalize }}", {"v": "hELLO"}), "Hello")
        self.assertEqual(render("{{ v | strip }}", {"v": "  x  "}), "x")
        self.assertEqual(render("{{ v | lstrip }}", {"v": "  x  "}), "x  ")
        self.assertEqual(render("{{ v | rstrip }}", {"v": "  x  "}), "  x")

    def test_affix_and_replace_filters(self):
        self.assertEqual(render("{{ v | append: '!' }}", {"v": "a"}), "a!")
        self.assertEqual(render("{{ v | prepend: '> ' }}", {"v": "a"}), "> a")
        self.assertEqual(
            render("{{ v | replace: 'a', 'b' }}", {"v": "a-a"}), "b-b")
        self.assertEqual(
            render("{{ v | replace_first: 'a', 'b' }}", {"v": "a-a"}), "b-a")
        self.assertEqual(
            render("{{ v | replace_last: 'a', 'b' }}", {"v": "a-a"}), "a-b")
        self.assertEqual(render("{{ v | remove: 'a' }}", {"v": "a-a"}), "-")
        self.assertEqual(
            render("{{ v | remove_first: 'a' }}", {"v": "a-a"}), "-a")
        self.assertEqual(
            render("{{ v | remove_last: 'a' }}", {"v": "a-a"}), "a-")

    def test_truncate_and_split_join(self):
        self.assertEqual(
            render("{{ v | truncate: 8 }}", {"v": "abcdefghij"}), "abcde...")
        self.assertEqual(
            render("{{ v | truncate: 20 }}", {"v": "abcdefghij"}), "abcdefghij")
        self.assertEqual(
            render("{{ v | truncatewords: 2 }}", {"v": "a b c d"}),
            "a b...",
        )
        self.assertEqual(
            render("{{ v | split: ',' | join: '|' }}", {"v": "a,b,c"}),
            "a|b|c",
        )

    def test_newline_and_escaping_filters(self):
        self.assertEqual(
            render("{{ v | newline_to_br }}", {"v": "a\nb"}),
            "a<br />\nb",
        )
        self.assertEqual(
            render("{{ v | strip_newlines }}", {"v": "a\nb\r\n"}), "ab")
        self.assertEqual(
            render("{{ v | escape }}", {"v": "<a & 'b'>"}),
            "&lt;a &amp; &#39;b&#39;&gt;",
        )
        self.assertEqual(
            render("{{ v | escape_once }}", {"v": "&lt;b&gt; & c"}),
            "&lt;b&gt; &amp; c",
        )
        self.assertEqual(
            render("{{ v | url_encode }}", {"v": "a b&c"}), "a+b%26c")
        self.assertEqual(
            render("{{ v | url_decode }}", {"v": "a+b%26c"}), "a b&c")

    def test_default_and_size(self):
        self.assertEqual(render("{{ v | default: 'x' }}", {"v": None}), "x")
        self.assertEqual(render("{{ v | default: 'x' }}", {"v": ""}), "x")
        self.assertEqual(render("{{ v | default: 'x' }}", {"v": []}), "x")
        self.assertEqual(render("{{ v | default: 'x' }}", {"v": "y"}), "y")
        self.assertEqual(render("{{ v | size }}", {"v": "abc"}), "3")
        self.assertEqual(render("{{ v | size }}", {"v": [1, 2]}), "2")


class CollectionFilterTest(unittest.TestCase):
    def test_first_last_slice(self):
        self.assertEqual(render("{{ v | first }}", {"v": ["a", "b"]}), "a")
        self.assertEqual(render("{{ v | last }}", {"v": ["a", "b"]}), "b")
        self.assertEqual(render("{{ v | slice: 1, 2 }}", {"v": "abcd"}), "bc")
        self.assertEqual(
            render("{{ v | slice: -2, 2 }}", {"v": ["a", "b", "c"]}),
            "bc",
        )

    def test_map_where_and_join(self):
        values = {"items": [
            {"name": "b", "ok": True},
            {"name": "a", "ok": False},
        ]}
        self.assertEqual(
            render("{{ items | map: 'name' | join: ',' }}", values), "b,a")
        self.assertEqual(
            render("{{ items | where: 'ok', true | map: 'name' }}", values),
            "b",
        )
        self.assertEqual(
            render("{{ items | where: 'ok' | map: 'name' }}", values), "b")

    def test_sort_uniq_compact_reverse_concat(self):
        self.assertEqual(
            render("{{ v | sort | join: ',' }}", {"v": [10, 2, 1]}),
            "1,2,10",
        )
        self.assertEqual(
            render("{{ v | sort_natural | join: ',' }}", {"v": ["b", "A"]}),
            "A,b",
        )
        self.assertEqual(
            render("{{ v | uniq | join: ',' }}", {"v": ["a", "a", "b"]}),
            "a,b",
        )
        self.assertEqual(
            render("{{ v | compact | join: ',' }}", {"v": ["a", None, "b"]}),
            "a,b",
        )
        self.assertEqual(
            render("{{ v | reverse | join: ',' }}", {"v": ["a", "b"]}),
            "b,a",
        )
        self.assertEqual(
            render("{{ a | concat: b | join: ',' }}",
                   {"a": ["a"], "b": ["b", "c"]}),
            "a,b,c",
        )


class NumberFilterTest(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(render("{{ n | plus: 2 }}", {"n": 3}), "5")
        self.assertEqual(render("{{ n | minus: 2 }}", {"n": 3}), "1")
        self.assertEqual(render("{{ n | times: 2 }}", {"n": 3}), "6")
        self.assertEqual(render("{{ n | divided_by: 2 }}", {"n": 7}), "3.5")
        self.assertEqual(render("{{ n | modulo: 2 }}", {"n": 7}), "1")
        self.assertEqual(
            render("{{ n | plus: 2 | times: 3 }}", {"n": 4}), "18")

    def test_rounding_and_bounds(self):
        self.assertEqual(render("{{ n | abs }}", {"n": -3}), "3")
        self.assertEqual(render("{{ n | ceil }}", {"n": 1.2}), "2")
        self.assertEqual(render("{{ n | floor }}", {"n": 1.8}), "1")
        self.assertEqual(render("{{ n | round: 1 }}", {"n": 1.25}), "1.2")
        self.assertEqual(render("{{ n | at_least: 5 }}", {"n": 3}), "5")
        self.assertEqual(render("{{ n | at_most: 5 }}", {"n": 7}), "5")

    def test_division_by_zero_is_an_error(self):
        with self.assertRaises(TemplateError):
            render("{{ n | divided_by: 0 }}", {"n": 1})
        with self.assertRaises(TemplateError):
            render("{{ n | modulo: 0 }}", {"n": 1})

    def test_date_and_json(self):
        self.assertEqual(
            render("{{ v | date: '%Y-%m-%d %H:%M' }}",
                   {"v": "2026-11-14T10:20:30Z"}),
            "2026-11-14 10:20",
        )
        self.assertEqual(
            render("{{ v | json }}", {"v": {"a": 1}}), '{"a": 1}')


class StrictFilterTest(unittest.TestCase):
    def test_unknown_filter_fails_like_solid_strict_filters(self):
        with self.assertRaises(TemplateError) as error:
            render("{{ v | no_such_filter }}", {"v": "x"})
        self.assertIn("unknown Liquid filter: no_such_filter",
                      str(error.exception))

    def test_undefined_variable_still_fails(self):
        with self.assertRaises(TemplateError):
            render("{{ missing | upcase }}", {})

    def test_render_does_not_mutate_caller_context(self):
        context = {"v": "a"}
        render("{% assign v = 'b' %}{{ v }}", context)
        self.assertEqual(context, {"v": "a"})


class TagTest(unittest.TestCase):
    def test_assign_and_capture(self):
        self.assertEqual(
            render("{% assign x = v | upcase %}{{ x }}", {"v": "ab"}), "AB")
        self.assertEqual(
            render("{% capture g %}hi {{ v }}{% endcapture %}{{ g }}!",
                   {"v": "bo"}),
            "hi bo!",
        )

    def test_case_with_multiple_values_and_else(self):
        template_source = (
            "{% case v %}{% when 'a' %}A{% when 'b' or 'c' %}BC"
            "{% else %}other{% endcase %}"
        )
        self.assertEqual(render(template_source, {"v": "a"}), "A")
        self.assertEqual(render(template_source, {"v": "c"}), "BC")
        self.assertEqual(render(template_source, {"v": "z"}), "other")

    def test_case_with_comma_separated_values(self):
        self.assertEqual(
            render("{% case v %}{% when 'a', 'b' %}AB{% endcase %}", {"v": "b"}),
            "AB",
        )

    def test_raw_block_is_not_rendered(self):
        self.assertEqual(
            render("{% raw %}{{ v }}{% endraw %}", {"v": "x"}), "{{ v }}")

    def test_filter_inside_condition_and_loop(self):
        self.assertEqual(
            render("{% if v | size > 2 %}long{% else %}short{% endif %}",
                   {"v": "abc"}),
            "long",
        )
        self.assertEqual(
            render("{% for item in items | reverse %}{{ item }}{% endfor %}",
                   {"items": ["a", "b"]}),
            "ba",
        )

    def test_assign_inside_loop_stays_in_loop_scope(self):
        rendered = render(
            "{% for item in items %}{% assign last = item %}{{ last }}"
            "{% endfor %}",
            {"items": ["a", "b"]},
        )
        self.assertEqual(rendered, "ab")

    def test_unclosed_blocks_are_errors(self):
        for source in ("{% case v %}", "{% raw %}", "{% capture x %}"):
            with self.assertRaises(TemplateError):
                render(source, {"v": "a"})


class WorkflowTemplateTest(unittest.TestCase):
    def test_solid_style_workflow_prompt_renders(self):
        source = (
            "You are working on `{{ issue.identifier | upcase }}`\n"
            "{% if attempt %}Attempt {{ attempt | plus: 1 }}{% endif %}\n"
            "{% for label in issue.labels %}[{{ label | downcase }}]"
            "{% endfor %}\n"
            "{{ issue.description | default: 'No description' }}"
        )
        issue = {
            "identifier": "yil-1",
            "labels": ["Backend", "Urgent"],
            "description": None,
        }
        self.assertEqual(
            render(source, {"issue": issue, "attempt": None}),
            "You are working on `YIL-1`\n\n[backend][urgent]\n"
            "No description",
        )


if __name__ == "__main__":
    unittest.main()
