#!/usr/bin/env python3
"""Generate a self-hosted GitHub language-usage card.

For every eligible repository, languages that account for more than 10% of
that repository's code are kept. The kept languages are then aggregated by
their raw byte counts across all repositories, and a language's ratio is:

    total bytes of that language in kept entries
    --------------------------------------------
       total bytes of all kept language entries

By default only public, non-fork repositories owned by the user are counted.
No personal access token is required when run from GitHub Actions: the
ephemeral GITHUB_TOKEN is enough for public repository discovery and for the
per-repository language endpoints.
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import html
import json
import os
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "self-hosted-language-card/1.0"
PER_PAGE = 100
DEFAULT_OUTPUT = "assets/languages.svg"
DEFAULT_MAX_LANGUAGES = 9
DEFAULT_MIN_REPO_SHARE = 10.0

# GitHub Linguist colors for common languages. Unknown languages get a stable
# color derived from their name, so the card never depends on an external
# color service.
LANGUAGE_COLORS: dict[str, str] = {
    "ada": "#02f88c",
    "apex": "#1797c0",
    "assembly": "#6e4c13",
    "batchfile": "#c1f12e",
    "c": "#555555",
    "c#": "#178600",
    "c++": "#f34b7d",
    "clojure": "#db5855",
    "coffeescript": "#244776",
    "common lisp": "#3fb68b",
    "crystal": "#000100",
    "css": "#563d7c",
    "d": "#ba595e",
    "dart": "#00b4ab",
    "delphi": "#b0ce4e",
    "dockerfile": "#384d54",
    "elixir": "#6e4a7e",
    "elm": "#60b5cc",
    "emacs lisp": "#c065db",
    "erlang": "#b83998",
    "f#": "#b845fc",
    "fortran": "#4d41b1",
    "gdscript": "#355570",
    "go": "#00add8",
    "groovy": "#4298b8",
    "hack": "#878787",
    "haskell": "#5e5086",
    "hcl": "#844fba",
    "html": "#e34c26",
    "java": "#b07219",
    "javascript": "#f1e05a",
    "julia": "#a270ba",
    "jupyter notebook": "#da5b0b",
    "kotlin": "#a97bff",
    "lua": "#000080",
    "makefile": "#427819",
    "matlab": "#e16737",
    "nim": "#ffc200",
    "nix": "#7e7eff",
    "objective-c": "#438eff",
    "ocaml": "#ef7a08",
    "pascal": "#e3f171",
    "perl": "#0298c3",
    "php": "#4f5d95",
    "plpgsql": "#336790",
    "powershell": "#012456",
    "prolog": "#74283c",
    "python": "#3572a5",
    "r": "#198ce7",
    "raku": "#0000fb",
    "ruby": "#701516",
    "rust": "#dea584",
    "scala": "#c22d40",
    "scheme": "#1e4aec",
    "scss": "#c6538c",
    "shell": "#89e051",
    "solidity": "#aa6746",
    "svelte": "#ff3e00",
    "swift": "#f05138",
    "tex": "#3d6117",
    "tsql": "#e38c00",
    "typescript": "#3178c6",
    "vala": "#a56de2",
    "vim script": "#199f4b",
    "visual basic .net": "#945db7",
    "vue": "#41b883",
    "zig": "#ec915c",
}


class GitHubApiError(RuntimeError):
    """A non-retryable GitHub API error."""


class RateLimitError(GitHubApiError):
    """The GitHub API rate limit has been exhausted."""


@dataclass(frozen=True)
class LanguageRow:
    name: str
    byte_count: int
    percentage: float
    color: str
    is_other: bool = False


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def api_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _read_json_response(request: Request) -> tuple[Any, Mapping[str, str]]:
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")), response.headers
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # pragma: no cover - defensive
            detail = ""
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
        if exc.code in {403, 429} and remaining == "0":
            when = f" (resets at Unix timestamp {reset})" if reset else ""
            raise RateLimitError(
                f"GitHub API rate limit exhausted{when}. Configure a token with "
                "more quota, or wait for the limit to reset."
            ) from exc
        raise GitHubApiError(
            f"GitHub API returned HTTP {exc.code} for {request.full_url}: {detail}"
        ) from exc
    except URLError as exc:
        raise GitHubApiError(f"Could not reach GitHub API: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise GitHubApiError("GitHub API returned invalid JSON") from exc


def api_get(path: str, token: str | None, *, retries: int = 3) -> Any:
    """GET a GitHub API path with small retries for transient failures."""
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    last_error: Exception | None = None
    for attempt in range(retries):
        request = Request(url, headers=api_headers(token), method="GET")
        try:
            data, _headers = _read_json_response(request)
            return data
        except RateLimitError:
            raise
        except GitHubApiError as exc:
            last_error = exc
            # Retry only errors that are likely transient.
            transient = any(
                marker in str(exc)
                for marker in (
                    "HTTP 429",
                    "HTTP 500",
                    "HTTP 502",
                    "HTTP 503",
                    "HTTP 504",
                    "Could not reach GitHub API",
                )
            )
            if not transient or attempt == retries - 1:
                raise
            time.sleep(2**attempt)
    raise GitHubApiError(str(last_error))  # pragma: no cover


def list_repositories(
    username: str,
    token: str | None,
    *,
    include_private: bool,
) -> list[dict[str, Any]]:
    """List repositories owned by username, with pagination.

    The public path works with the automatically-provided GITHUB_TOKEN. The
    private path deliberately requires a user-provided token and verifies that
    the token belongs to the configured username before enumerating repos.
    """
    if include_private:
        if not token:
            raise GitHubApiError(
                "INCLUDE_PRIVATE=true requires a token with private-repo access."
            )
        current_user = api_get("/user", token)
        current_login = str(current_user.get("login", ""))
        if current_login.casefold() != username.casefold():
            raise GitHubApiError(
                "The supplied token belongs to "
                f"@{current_login}, not @{username}. Refusing to list private repos."
            )
        endpoint = "/user/repos"
        base_params = {
            "affiliation": "owner",
            "visibility": "all",
            "sort": "updated",
            "per_page": PER_PAGE,
        }
    else:
        endpoint = f"/users/{quote(username, safe='')}/repos"
        base_params = {
            "type": "owner",
            "sort": "updated",
            "per_page": PER_PAGE,
        }

    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {**base_params, "page": page}
        data = api_get(f"{endpoint}?{urlencode(params)}", token)
        if not isinstance(data, list):
            raise GitHubApiError(f"Unexpected repository-list response: {type(data)!r}")
        for repo in data:
            if isinstance(repo, dict) and repo.get("full_name"):
                repos.append(repo)
        if len(data) < PER_PAGE:
            break
        page += 1
    return repos


def fetch_repo_languages(full_name: str, token: str | None) -> dict[str, int]:
    data = api_get(f"/repos/{full_name}/languages", token)
    if not isinstance(data, dict):
        raise GitHubApiError(f"Unexpected language response for {full_name}")
    return {
        str(language): int(byte_count)
        for language, byte_count in data.items()
        if isinstance(byte_count, (int, float)) and byte_count > 0
    }


def should_include_repo(
    repo: Mapping[str, Any],
    *,
    include_forks: bool,
    include_archived: bool,
    exclude_repos: set[str],
) -> bool:
    if repo.get("disabled"):
        return False
    if repo.get("fork") and not include_forks:
        return False
    if repo.get("archived") and not include_archived:
        return False
    full_name = str(repo.get("full_name", ""))
    name = str(repo.get("name", ""))
    excluded = {item.casefold() for item in exclude_repos}
    return full_name.casefold() not in excluded and name.casefold() not in excluded


def language_color(name: str) -> str:
    known = LANGUAGE_COLORS.get(name.casefold())
    if known:
        return known
    digest = hashlib.sha256(name.casefold().encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535
    saturation = 0.55 + (digest[2] / 255) * 0.20
    lightness = 0.46 + (digest[3] / 255) * 0.10
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def aggregate_languages(
    per_repo_languages: Iterable[Mapping[str, int]],
    *,
    max_languages: int = DEFAULT_MAX_LANGUAGES,
    min_repo_share: float = DEFAULT_MIN_REPO_SHARE,
) -> tuple[list[LanguageRow], int, int, int]:
    """Aggregate byte-weighted shares of each repository's major languages.

    A language must account for strictly more than ``min_repo_share`` percent
    of its own repository to be included. Included byte counts are then summed
    across repositories. The returned percentage is therefore the language's
    share of all included code bytes, not a count of top-1/top-2 appearances.

    Returns:
        (rows, included_bytes, repositories_with_languages,
         contributing_repositories)
    """
    bytes_by_language: Counter[str] = Counter()
    repos_with_languages = 0
    contributing_repositories = 0

    for languages in per_repo_languages:
        positive = {
            str(name): int(byte_count)
            for name, byte_count in languages.items()
            if isinstance(byte_count, (int, float)) and byte_count > 0
        }
        if not positive:
            continue
        repos_with_languages += 1
        repository_total = sum(positive.values())
        included = [
            (name, byte_count)
            for name, byte_count in positive.items()
            if repository_total > 0
            and byte_count / repository_total * 100 > min_repo_share
        ]
        if not included:
            continue
        contributing_repositories += 1
        for name, byte_count in included:
            bytes_by_language[name] += byte_count

    total_bytes = sum(bytes_by_language.values())
    if total_bytes == 0:
        return [], 0, repos_with_languages, 0

    ranked_languages = sorted(
        bytes_by_language.items(),
        key=lambda item: (-item[1], item[0].casefold()),
    )

    if max_languages < 1:
        max_languages = 1

    if len(ranked_languages) <= max_languages:
        selected = ranked_languages
        other_bytes = 0
    else:
        # Keep one slot for an "other" aggregate, unless max_languages is 1.
        keep = max_languages - 1 if max_languages > 1 else max_languages
        selected = ranked_languages[:keep]
        other_bytes = sum(byte_count for _, byte_count in ranked_languages[keep:])

    rows: list[LanguageRow] = [
        LanguageRow(
            name=name,
            byte_count=byte_count,
            percentage=byte_count / total_bytes * 100,
            color=language_color(name),
        )
        for name, byte_count in selected
    ]
    if other_bytes:
        rows.append(
            LanguageRow(
                name="其他",
                byte_count=other_bytes,
                percentage=other_bytes / total_bytes * 100,
                color="#8b949e",
                is_other=True,
            )
        )
    return rows, total_bytes, repos_with_languages, contributing_repositories

def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _truncate(value: str, limit: int = 26) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _percent(value: float) -> str:
    if 0 < value < 0.05:
        return "<0.1%"
    return f"{value:.1f}%"


def render_svg(
    *,
    username: str,
    rows: list[LanguageRow],
    repo_count: int,
    total_bytes: int,
    min_repo_share: float,
    scope_label: str,
    title: str = "语言使用占比",
) -> str:
    """Render a dependency-free, script-free SVG card."""
    card_width = 520
    bar_x = 24
    bar_width = card_width - bar_x * 2
    row_height = 36
    first_row_y = 92
    row_count = max(len(rows), 1)
    height = 114 + row_count * row_height

    title_text = _text(title)
    threshold = f"{min_repo_share:g}%"
    subtitle = (
        f"@{_text(username)} · {repo_count} 个{_text(scope_label)}中"
        f"代码占比 >{_text(threshold)} 的语言"
    )
    footer = (
        f"仅统计仓库内代码占比 >{threshold} 的语言 · 按代码字节加权"
        if total_bytes
        else "尚未找到可统计的语言数据"
    )

    parts: list[str] = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{card_width}" '
            f'height="{height}" viewBox="0 0 {card_width} {height}" '
            'role="img" aria-labelledby="card-title card-desc">'
        ),
        f"<title id=\"card-title\">@{_text(username)} 的语言使用占比</title>",
        (
            f'<desc id="card-desc">按每个仓库内代码占比超过 {_text(threshold)} '
            f'的语言字节数加权统计。{_text(footer)}</desc>'
        ),
        "<style>",
        (
            '.card{fill:#ffffff;stroke:#d0d7de;stroke-width:1}'
            '.title{fill:#1f2328;font:600 19px -apple-system,BlinkMacSystemFont,'
            '"Segoe UI","Noto Sans","PingFang SC","Microsoft YaHei",sans-serif}'
            '.subtitle{fill:#59636e;font:12px -apple-system,BlinkMacSystemFont,'
            '"Segoe UI","Noto Sans","PingFang SC","Microsoft YaHei",sans-serif}'
            '.lang{fill:#1f2328;font:13px -apple-system,BlinkMacSystemFont,'
            '"Segoe UI","Noto Sans","PingFang SC","Microsoft YaHei",sans-serif}'
            '.pct{fill:#59636e;font:600 12px -apple-system,BlinkMacSystemFont,'
            '"Segoe UI","Noto Sans",sans-serif}'
            '.track{fill:#eaeef2}'
            '.footer{fill:#59636e;font:11px -apple-system,BlinkMacSystemFont,'
            '"Segoe UI","Noto Sans","PingFang SC","Microsoft YaHei",sans-serif}'
            '@media (prefers-color-scheme:dark){'
            '.card{fill:#0d1117;stroke:#30363d}'
            '.title,.lang{fill:#e6edf3}'
            '.subtitle,.pct,.footer{fill:#8b949e}'
            '.track{fill:#21262d}}'
        ),
        "</style>",
        f'<rect class="card" x="0.5" y="0.5" width="{card_width - 1}" '
        f'height="{height - 1}" rx="10"/>',
        f'<text class="title" x="{bar_x}" y="34">{title_text}</text>',
        f'<text class="subtitle" x="{bar_x}" y="59">{subtitle}</text>',
    ]

    if not rows:
        parts.append(
            f'<text class="lang" x="{bar_x}" y="{first_row_y}">'
            f"没有语言的代码占比超过 {_text(threshold)}。</text>"
        )
    else:
        for index, row in enumerate(rows):
            row_y = first_row_y + index * row_height
            bar_y = row_y + 9
            filled_width = max(0.0, bar_width * row.percentage / 100)
            label = _text(_truncate(row.name))
            pct = _text(_percent(row.percentage))
            parts.append(
                f'<text class="lang" x="{bar_x}" y="{row_y}">{label}</text>'
            )
            parts.append(
                f'<text class="pct" x="{card_width - bar_x}" y="{row_y}" '
                f'text-anchor="end">{pct}</text>'
            )
            parts.append(
                f'<rect class="track" x="{bar_x}" y="{bar_y}" '
                f'width="{bar_width}" height="8" rx="4"/>'
            )
            parts.append(
                f'<rect x="{bar_x}" y="{bar_y}" width="{filled_width:.2f}" '
                f'height="8" rx="4" fill="{row.color}"/>'
            )

    parts.append(
        f'<text class="footer" x="{bar_x}" y="{height - 16}">{_text(footer)}</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def generate(
    *,
    username: str,
    token: str | None,
    output: Path,
    include_private: bool,
    include_forks: bool,
    include_archived: bool,
    exclude_repos: set[str],
    max_languages: int,
    min_repo_share: float,
    title: str,
    workers: int,
) -> tuple[list[LanguageRow], int, int]:
    all_repos = list_repositories(username, token, include_private=include_private)
    repos = [
        repo
        for repo in all_repos
        if should_include_repo(
            repo,
            include_forks=include_forks,
            include_archived=include_archived,
            exclude_repos=exclude_repos,
        )
    ]

    per_repo_languages: list[Mapping[str, int]] = []
    skipped: list[str] = []
    if repos:
        max_workers = max(1, min(workers, len(repos)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_repo = {
                executor.submit(fetch_repo_languages, str(repo["full_name"]), token): repo
                for repo in repos
            }
            for future in as_completed(future_to_repo):
                repo = future_to_repo[future]
                full_name = str(repo["full_name"])
                try:
                    languages = future.result()
                except RateLimitError:
                    for pending in future_to_repo:
                        pending.cancel()
                    raise
                except GitHubApiError as exc:
                    skipped.append(f"{full_name}: {exc}")
                    continue
                if languages:
                    per_repo_languages.append(languages)

    rows, total_bytes, _repos_with_languages, contributing_repos = aggregate_languages(
        per_repo_languages,
        max_languages=max_languages,
        min_repo_share=min_repo_share,
    )

    scope_label = "仓库" if include_private else "公开仓库"
    svg = render_svg(
        username=username,
        rows=rows,
        repo_count=contributing_repos,
        total_bytes=total_bytes,
        min_repo_share=min_repo_share,
        scope_label=scope_label,
        title=title,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(svg, encoding="utf-8")
    temporary.replace(output)

    if skipped:
        print(
            f"警告：跳过了 {len(skipped)} 个仓库的语言查询。",
            file=sys.stderr,
        )
        for line in skipped[:10]:
            print(f"  - {line}", file=sys.stderr)
    return rows, total_bytes, contributing_repos


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--username",
        default=os.getenv("GH_USERNAME") or os.getenv("GITHUB_REPOSITORY_OWNER"),
        help="GitHub username (default: GH_USERNAME or GITHUB_REPOSITORY_OWNER)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.getenv("LANGUAGE_STATS_OUTPUT", DEFAULT_OUTPUT)),
        help=f"SVG output path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--max-languages",
        type=int,
        default=int(os.getenv("LANGUAGE_STATS_MAX_ROWS", DEFAULT_MAX_LANGUAGES)),
        help=f"Maximum rows before grouping into 其他 (default: {DEFAULT_MAX_LANGUAGES})",
    )
    parser.add_argument(
        "--min-repo-share",
        type=float,
        default=float(
            os.getenv("LANGUAGE_STATS_MIN_REPO_SHARE", str(DEFAULT_MIN_REPO_SHARE))
        ),
        help=(
            "Include a language only when it exceeds this percentage inside "
            f"its own repository (default: {DEFAULT_MIN_REPO_SHARE:g}%%)."
        ),
    )
    parser.add_argument(
        "--title",
        default=os.getenv("LANGUAGE_STATS_TITLE", "语言使用占比"),
        help="Card title",
    )
    parser.add_argument(
        "--include-forks",
        action=argparse.BooleanOptionalAction,
        default=env_bool("LANGUAGE_STATS_INCLUDE_FORKS", False),
        help="Count forked repositories too (default: false)",
    )
    parser.add_argument(
        "--include-archived",
        action=argparse.BooleanOptionalAction,
        default=env_bool("LANGUAGE_STATS_INCLUDE_ARCHIVED", True),
        help="Count archived repositories too (default: true)",
    )
    parser.add_argument(
        "--include-private",
        action=argparse.BooleanOptionalAction,
        default=env_bool("LANGUAGE_STATS_INCLUDE_PRIVATE", False),
        help="Count private repos; requires a token with repo access (default: false)",
    )
    parser.add_argument(
        "--exclude-repos",
        default=os.getenv("LANGUAGE_STATS_EXCLUDE_REPOS", ""),
        help="Comma-separated owner/repo or bare repository names to skip",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("LANGUAGE_STATS_WORKERS", "6")),
        help="Concurrent language API requests (default: 6)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    username = (args.username or "").strip().lstrip("@")
    if not username:
        print(
            "错误：请设置 GH_USERNAME、GITHUB_REPOSITORY_OWNER 或 --username。",
            file=sys.stderr,
        )
        return 2
    if args.max_languages < 1:
        print("错误：--max-languages 必须大于等于 1。", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("错误：--workers 必须大于等于 1。", file=sys.stderr)
        return 2
    if not 0 <= args.min_repo_share < 100:
        print("错误：--min-repo-share 必须在 [0, 100) 区间内。", file=sys.stderr)
        return 2

    token = os.getenv("LANGUAGE_STATS_TOKEN") or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    exclude_repos = {
        item.strip() for item in args.exclude_repos.split(",") if item.strip()
    }
    try:
        rows, total, repo_count = generate(
            username=username,
            token=token,
            output=args.output,
            include_private=args.include_private,
            include_forks=args.include_forks,
            include_archived=args.include_archived,
            exclude_repos=exclude_repos,
            max_languages=args.max_languages,
            min_repo_share=args.min_repo_share,
            title=args.title,
            workers=args.workers,
        )
    except (GitHubApiError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(
        f"已生成 {args.output}：{len(rows)} 行，{repo_count} 个仓库参与统计，"
        f"计入 {total} 字节。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
