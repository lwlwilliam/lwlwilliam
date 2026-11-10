from __future__ import annotations

import json
import unittest

from scripts.generate_language_stats import (
    aggregate_languages,
    language_color,
    parse_cloc_json,
    render_svg,
    should_include_repo,
)


class AggregateLanguagesTests(unittest.TestCase):
    def test_sums_lines_across_repositories(self) -> None:
        rows, total, repos, language_count = aggregate_languages(
            [
                {"Python": 700, "Shell": 200},
                {"Python": 100, "TypeScript": 800},
            ],
            max_languages=10,
        )
        self.assertEqual(total, 1800)
        self.assertEqual(repos, 2)
        self.assertEqual(language_count, 3)
        by_name = {row.name: row for row in rows}
        self.assertEqual(by_name["TypeScript"].line_count, 800)
        self.assertEqual(by_name["Python"].line_count, 800)
        self.assertAlmostEqual(by_name["Python"].percentage, 800 / 1800 * 100)
        self.assertAlmostEqual(sum(row.percentage for row in rows), 100.0)

    def test_percentage_uses_all_languages_but_displays_top_n(self) -> None:
        rows, total, _repos, language_count = aggregate_languages(
            [
                {"A": 100, "B": 100},
                {"C": 100, "D": 100},
                {"E": 100},
            ],
            max_languages=2,
        )
        self.assertEqual(total, 500)
        self.assertEqual(language_count, 5)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.name for row in rows}, {"A", "B"})
        self.assertAlmostEqual(rows[0].percentage, 20.0)
        self.assertLess(sum(row.percentage for row in rows), 100.0)

    def test_excludes_markup_docs_data_and_sql_languages(self) -> None:
        rows, total, repos, language_count = aggregate_languages(
            [
                {
                    "Python": 100,
                    "HTML": 500,
                    "CSS": 500,
                    "SQL": 100,
                    "Markdown": 300,
                    "YAML": 50,
                }
            ],
            exclude_languages={"HTML", "CSS", "SQL", "Markdown", "YAML"},
        )
        self.assertEqual(total, 100)
        self.assertEqual(repos, 1)
        self.assertEqual(language_count, 1)
        self.assertEqual([(row.name, row.line_count) for row in rows], [("Python", 100)])

    def test_empty_input(self) -> None:
        rows, total, repos, language_count = aggregate_languages([{}, {"A": 0}])
        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertEqual(repos, 0)
        self.assertEqual(language_count, 0)

    def test_ties_are_deterministic(self) -> None:
        rows, _total, _repos, _language_count = aggregate_languages(
            [{"Zig": 10}, {"Ada": 10}]
        )
        self.assertEqual([row.name for row in rows], ["Ada", "Zig"])


class ClocParsingTests(unittest.TestCase):
    def test_parses_code_lines_and_ignores_metadata(self) -> None:
        payload = {
            "header": {"cloc_version": "1.0"},
            "Python": {"blank": 10, "comment": 20, "code": 300},
            "JavaScript": {"blank": 1, "comment": 2, "code": 100},
            "SUM": {"blank": 11, "comment": 22, "code": 400},
        }
        self.assertEqual(
            parse_cloc_json(payload),
            {"Python": 300, "JavaScript": 100},
        )

    def test_parses_json_with_leading_warning_text(self) -> None:
        payload = "some warning\n" + json.dumps(
            {"Python": {"code": 42}, "SUM": {"code": 42}}
        )
        self.assertEqual(parse_cloc_json(payload), {"Python": 42})


class RenderTests(unittest.TestCase):
    def test_svg_escapes_dynamic_text(self) -> None:
        rows, total, repos, language_count = aggregate_languages(
            [{"<script>": 10, "A&B": 1}]
        )
        svg = render_svg(
            username='user<&"',
            rows=rows,
            repo_count=repos,
            total_lines=total,
            language_count=language_count,
            scope_label="公开及私有仓库",
        )
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)
        self.assertIn("user&lt;&amp;&quot;", svg)
        self.assertTrue(svg.startswith("<svg"))

    def test_footer_shows_cloc_and_top_n(self) -> None:
        rows, total, repos, language_count = aggregate_languages(
            [{"Python": 100}, {"Go": 100}, {"Rust": 100}],
            max_languages=2,
        )
        svg = render_svg(
            username="alice",
            rows=rows,
            repo_count=repos,
            total_lines=total,
            language_count=language_count,
            scope_label="公开及私有仓库",
        )
        self.assertIn("cloc", svg)
        self.assertIn("共 3 种语言", svg)
        self.assertIn("显示前 2", svg)

    def test_unknown_language_has_stable_color(self) -> None:
        self.assertEqual(language_color("SomeNewLang"), language_color("somenewlang"))
        self.assertRegex(language_color("SomeNewLang"), r"^#[0-9a-f]{6}$")


class RepositoryFilterTests(unittest.TestCase):
    def test_filters_forks_and_disabled_by_default(self) -> None:
        self.assertFalse(
            should_include_repo(
                {"full_name": "a/a", "name": "a", "fork": True},
                include_forks=False,
                include_archived=True,
                exclude_repos=set(),
            )
        )
        self.assertFalse(
            should_include_repo(
                {"full_name": "a/b", "name": "b", "disabled": True},
                include_forks=True,
                include_archived=True,
                exclude_repos=set(),
            )
        )
        self.assertTrue(
            should_include_repo(
                {"full_name": "a/c", "name": "c"},
                include_forks=False,
                include_archived=True,
                exclude_repos=set(),
            )
        )

    def test_exclude_list_accepts_bare_name_or_full_name(self) -> None:
        self.assertFalse(
            should_include_repo(
                {"full_name": "alice/demo", "name": "demo"},
                include_forks=False,
                include_archived=True,
                exclude_repos={"DEMO"},
            )
        )
        self.assertFalse(
            should_include_repo(
                {"full_name": "alice/demo", "name": "demo"},
                include_forks=False,
                include_archived=True,
                exclude_repos={"alice/demo"},
            )
        )


if __name__ == "__main__":
    unittest.main()
