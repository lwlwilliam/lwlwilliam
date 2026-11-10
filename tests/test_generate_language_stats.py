from __future__ import annotations

import unittest

from scripts.generate_language_stats import (
    aggregate_languages,
    language_color,
    render_svg,
    should_include_repo,
)


class AggregateLanguagesTests(unittest.TestCase):
    def test_keeps_languages_above_threshold_and_weights_by_bytes(self) -> None:
        rows, total, repos, contributing = aggregate_languages(
            [
                # Python is 80%; Shell and HTML are exactly 10% and excluded.
                {"Python": 800, "Shell": 100, "HTML": 100},
                # TypeScript is 90%; Python is exactly 10% and excluded.
                {"Python": 100, "TypeScript": 900},
            ],
            min_repo_share=10,
        )
        self.assertEqual(total, 1700)
        self.assertEqual(repos, 2)
        self.assertEqual(contributing, 2)
        by_name = {row.name: row for row in rows}
        self.assertEqual(set(by_name), {"Python", "TypeScript"})
        self.assertEqual(by_name["Python"].byte_count, 800)
        self.assertAlmostEqual(by_name["Python"].percentage, 800 / 1700 * 100)
        self.assertAlmostEqual(by_name["TypeScript"].percentage, 900 / 1700 * 100)
        self.assertAlmostEqual(sum(row.percentage for row in rows), 100.0)

    def test_filters_small_languages_before_aggregating(self) -> None:
        rows, total, _repos, _contributing = aggregate_languages(
            [{"A": 60, "B": 30, "C": 10}],
            min_repo_share=10,
        )
        self.assertEqual(total, 90)
        self.assertEqual([(row.name, row.byte_count) for row in rows], [("A", 60), ("B", 30)])
        self.assertAlmostEqual(rows[0].percentage, 60 / 90 * 100)
        self.assertNotIn("C", {row.name for row in rows})

    def test_repository_without_any_above_threshold_is_skipped(self) -> None:
        rows, total, repos, contributing = aggregate_languages(
            [
                {"A": 10, "B": 10, "C": 10, "D": 10, "E": 10,
                 "F": 10, "G": 10, "H": 10, "I": 10, "J": 10},
            ],
            min_repo_share=10,
        )
        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertEqual(repos, 1)
        self.assertEqual(contributing, 0)

    def test_groups_extra_languages_into_other(self) -> None:
        rows, total, _repos, _contributing = aggregate_languages(
            [
                {"A": 100},
                {"B": 100},
                {"C": 100},
                {"D": 100},
            ],
            max_languages=3,
            min_repo_share=10,
        )
        self.assertEqual(total, 400)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1].name, "其他")
        self.assertEqual(rows[-1].byte_count, 200)
        self.assertAlmostEqual(sum(row.percentage for row in rows), 100.0)

    def test_empty_input(self) -> None:
        rows, total, repos, contributing = aggregate_languages([{}, {"A": 0}])
        self.assertEqual(rows, [])
        self.assertEqual(total, 0)
        self.assertEqual(repos, 0)
        self.assertEqual(contributing, 0)

    def test_ties_are_deterministic(self) -> None:
        rows, _total, _repos, _contributing = aggregate_languages(
            [{"Zig": 10}, {"Ada": 10}],
            min_repo_share=10,
        )
        self.assertEqual([row.name for row in rows], ["Ada", "Zig"])


class RenderTests(unittest.TestCase):
    def test_svg_escapes_dynamic_text(self) -> None:
        rows, total, repos, _contributing = aggregate_languages(
            [{"<script>": 10, "A&B": 1}],
            min_repo_share=0,
        )
        svg = render_svg(
            username='user<&"',
            rows=rows,
            repo_count=repos,
            total_bytes=total,
            min_repo_share=0,
            scope_label="公开仓库",
        )
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)
        self.assertIn("user&lt;&amp;&quot;", svg)
        self.assertTrue(svg.startswith("<svg"))

    def test_footer_shows_threshold_and_weighting(self) -> None:
        rows, total, repos, _contributing = aggregate_languages(
            [{"Python": 100}],
            min_repo_share=10,
        )
        svg = render_svg(
            username="alice",
            rows=rows,
            repo_count=repos,
            total_bytes=total,
            min_repo_share=10,
            scope_label="公开仓库",
        )
        self.assertIn("&gt;10%", svg)
        self.assertIn("按代码字节加权", svg)

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
