#!/usr/bin/env python3
"""Generate a self-hosted GitHub language-lines card.

Every eligible repository's default branch is downloaded and analysed with
cloc. For each language the effective lines of code are summed across all
repositories. The displayed percentage is:

    total effective lines for that language
    ---------------------------------------
      effective lines for all languages

The card lists the top languages by that percentage. Because cloc must inspect
the source, the run requires read access to every repository. Public-only usage
can use GitHub Actions' automatic GITHUB_TOKEN; including private repositories
requires a read-only token stored as a repository secret.
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter
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
DEFAULT_MAX_LANGUAGES = 10
DEFAULT_CLOC_EXCLUDES = (
    ".git,node_modules,vendor,dist,build,target,coverage,"
    ".venv,venv,__pycache__,Pods,.next,.nuxt"
)
# Non-programming / markup / data / documentation languages reported by cloc.
# Names are matched case-insensitively; users can override this list.
DEFAULT_EXCLUDE_LANGUAGES = (
    "Markdown,HTML,CSS,SCSS,Sass,Less,XML,YAML,JSON,TOML,INI,"
    "SQL,PL/SQL,T-SQL,TeX,reStructuredText,AsciiDoc,Text,"
    "Jupyter Notebook,GraphQL,XSLT,DTD,CSV,"
    "Makefile,make,Dockerfile,CMake,Meson,HCL,Protocol Buffers"
)

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
    line_count: int
    percentage: float
    color: str


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


class RepositoryUnavailableError(GitHubApiError):
    """A repository could not be downloaded or analysed."""


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


def download_repo_archive(
    full_name: str,
    token: str | None,
    destination: Path,
) -> None:
    """Download a repository's default-branch zipball through the API."""
    url = f"{API_ROOT}/repos/{quote(full_name, safe='/')}/zipball"
    request = Request(url, headers=api_headers(token), method="GET")
    try:
        with urlopen(request, timeout=180) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # pragma: no cover - defensive
            detail = ""
        if exc.code == 404:
            raise RepositoryUnavailableError(
                f"{full_name} has no downloadable default branch (404)."
            ) from exc
        remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
        reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
        if exc.code in {403, 429} and remaining == "0":
            when = f" (resets at Unix timestamp {reset})" if reset else ""
            raise RateLimitError(f"GitHub API rate limit exhausted{when}.") from exc
        raise RepositoryUnavailableError(
            f"Could not download {full_name}: HTTP {exc.code}. {detail}"
        ) from exc
    except URLError as exc:
        raise RepositoryUnavailableError(
            f"Could not download {full_name}: {exc.reason}"
        ) from exc


def extract_repo_archive(archive: Path, destination: Path) -> Path:
    """Extract a GitHub zipball and return its top-level repository directory."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            root = destination.resolve()
            for member in bundle.infolist():
                target = (destination / member.filename).resolve()
                if target != root and root not in target.parents:
                    raise RepositoryUnavailableError(
                        "Archive contains an unsafe path and was rejected."
                    )
            bundle.extractall(destination)
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise RepositoryUnavailableError("Downloaded archive is not a valid zip file.") from exc

    children = [path for path in destination.iterdir()]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return destination


def parse_cloc_json(payload: str | bytes | Mapping[str, Any]) -> dict[str, int]:
    """Return {language: effective code lines} from cloc's JSON output."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        start = payload.find("{")
        end = payload.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RepositoryUnavailableError("cloc did not return JSON.")
        try:
            data = json.loads(payload[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RepositoryUnavailableError("cloc returned invalid JSON.") from exc
    else:
        data = payload

    languages: dict[str, int] = {}
    for language, values in data.items():
        if language in {"header", "SUM"} or not isinstance(values, dict):
            continue
        code_lines = values.get("code")
        if isinstance(code_lines, (int, float)) and code_lines > 0:
            languages[str(language)] = int(code_lines)
    return languages


def count_code_lines(
    repository_directory: Path,
    *,
    cloc_bin: str,
    exclude_dirs: str,
    timeout_seconds: int,
) -> dict[str, int]:
    """Count effective source-code lines with cloc."""
    command = [cloc_bin, "--json", "--quiet", "--skip-uniqueness"]
    if exclude_dirs.strip():
        command.append(f"--exclude-dir={exclude_dirs.strip()}")
    command.append(str(repository_directory))

    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RepositoryUnavailableError(
            f"cloc executable not found: {cloc_bin}. Install it or set "
            "LANGUAGE_STATS_CLOC_BIN."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RepositoryUnavailableError(
            f"cloc timed out after {timeout_seconds}s for {repository_directory.name}."
        ) from exc

    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()[:500]
        raise RepositoryUnavailableError(
            f"cloc failed with exit code {process.returncode}: {detail}"
        )
    return parse_cloc_json(process.stdout)


def count_repository_lines(
    repo: Mapping[str, Any],
    *,
    token: str | None,
    cloc_bin: str,
    exclude_dirs: str,
    timeout_seconds: int,
) -> dict[str, int]:
    """Download, extract and line-count one repository's default branch."""
    full_name = str(repo.get("full_name", ""))
    if not full_name:
        raise RepositoryUnavailableError("Repository has no full_name.")
    if not repo.get("default_branch"):
        return {}

    temporary = Path(tempfile.mkdtemp(prefix="language-card-"))
    try:
        archive = temporary / "repository.zip"
        download_repo_archive(full_name, token, archive)
        repository_directory = extract_repo_archive(archive, temporary / "source")
        return count_code_lines(
            repository_directory,
            cloc_bin=cloc_bin,
            exclude_dirs=exclude_dirs,
            timeout_seconds=timeout_seconds,
        )
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def aggregate_languages(
    per_repo_lines: Iterable[Mapping[str, int]],
    *,
    max_languages: int = DEFAULT_MAX_LANGUAGES,
    exclude_languages: Iterable[str] = (),
) -> tuple[list[LanguageRow], int, int, int]:
    """Sum per-repository line counts and return the top languages.

    Percentages use the total line count of *all* aggregated languages as the
    denominator, including languages outside the displayed top N. Languages in
    ``exclude_languages`` are removed before the denominator is calculated.

    Returns:
        (rows, total_lines, repositories_with_code, language_count)
    """
    totals: Counter[str] = Counter()
    repositories_with_code = 0
    excluded = {
        str(language).strip().casefold()
        for language in exclude_languages
        if str(language).strip()
    }

    for repository_languages in per_repo_lines:
        cleaned = {
            str(language): int(line_count)
            for language, line_count in repository_languages.items()
            if (
                isinstance(line_count, (int, float))
                and line_count > 0
                and str(language).strip().casefold() not in excluded
            )
        }
        if cleaned:
            repositories_with_code += 1
            totals.update(cleaned)

    total_lines = sum(totals.values())
    if total_lines == 0:
        return [], 0, repositories_with_code, 0

    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0].casefold()))
    limit = max(1, max_languages)
    selected = ranked[:limit]
    rows = [
        LanguageRow(
            name=name,
            line_count=line_count,
            percentage=line_count / total_lines * 100,
            color=language_color(name),
        )
        for name, line_count in selected
    ]
    return rows, total_lines, repositories_with_code, len(ranked)


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
    total_lines: int,
    language_count: int,
    scope_label: str,
    title: str = "语言代码行数占比",
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
    subtitle = (
        f"@{_text(username)} · {repo_count} 个{_text(scope_label)}的代码行数"
    )
    if total_lines:
        shown_note = (
            f" · 显示前 {len(rows)}" if language_count > len(rows) else ""
        )
        footer = (
            f"cloc 有效代码行数 {total_lines:,} 行 · "
            f"共 {language_count} 种语言{shown_note}"
        )
    else:
        footer = "尚未找到可统计的代码行数"

    parts: list[str] = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{card_width}" '
            f'height="{height}" viewBox="0 0 {card_width} {height}" '
            'role="img" aria-labelledby="card-title card-desc">'
        ),
        f"<title id=\"card-title\">@{_text(username)} 的语言代码行数占比</title>",
        (
            '<desc id="card-desc">统计仓库默认分支中的有效代码行数，'
            '空行和注释不计入。cloc 结果按语言跨仓库累加。'
            f'{_text(footer)}</desc>'
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
            "尚未找到可统计的代码行数。</text>"
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
    exclude_languages: set[str],
    max_languages: int,
    title: str,
    workers: int,
    cloc_bin: str,
    cloc_exclude_dirs: str,
    cloc_timeout: int,
) -> tuple[list[LanguageRow], int, int, int, int, list[str]]:
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

    per_repo_lines: list[Mapping[str, int]] = []
    skipped: list[str] = []
    processed = 0
    if repos:
        max_workers = max(1, min(workers, len(repos)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_repo = {
                executor.submit(
                    count_repository_lines,
                    repo,
                    token=token,
                    cloc_bin=cloc_bin,
                    exclude_dirs=cloc_exclude_dirs,
                    timeout_seconds=cloc_timeout,
                ): repo
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
                except (GitHubApiError, OSError) as exc:
                    skipped.append(f"{full_name}: {exc}")
                    continue
                processed += 1
                if languages:
                    per_repo_lines.append(languages)
                    print(
                        f"已统计 {full_name}: "
                        f"{sum(languages.values()):,} 行，{len(languages)} 种语言",
                        flush=True,
                    )
                else:
                    print(f"已跳过 {full_name}: 没有可识别的源代码", flush=True)

    rows, total_lines, repo_count, language_count = aggregate_languages(
        per_repo_lines,
        max_languages=max_languages,
        exclude_languages=exclude_languages,
    )

    scope_label = "公开及私有仓库" if include_private else "公开仓库"
    svg = render_svg(
        username=username,
        rows=rows,
        repo_count=repo_count,
        total_lines=total_lines,
        language_count=language_count,
        scope_label=scope_label,
        title=title,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(svg, encoding="utf-8")
    temporary.replace(output)

    if skipped:
        print(
            f"警告：跳过了 {len(skipped)} 个仓库。",
            file=sys.stderr,
        )
        for line in skipped[:20]:
            print(f"  - {line}", file=sys.stderr)
        if len(skipped) > 20:
            print(f"  ... 另有 {len(skipped) - 20} 个", file=sys.stderr)
    return rows, total_lines, repo_count, language_count, processed, skipped


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
        help=f"Number of top languages to display (default: {DEFAULT_MAX_LANGUAGES})",
    )
    parser.add_argument(
        "--title",
        default=os.getenv("LANGUAGE_STATS_TITLE", "语言代码行数占比"),
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
        help="Count private repos; requires a repo-read token (default: false)",
    )
    parser.add_argument(
        "--exclude-repos",
        default=os.getenv("LANGUAGE_STATS_EXCLUDE_REPOS", ""),
        help="Comma-separated owner/repo or bare repository names to skip",
    )
    parser.add_argument(
        "--exclude-languages",
        default=os.getenv(
            "LANGUAGE_STATS_EXCLUDE_LANGUAGES", DEFAULT_EXCLUDE_LANGUAGES
        ),
        help=(
            "Comma-separated cloc language names to exclude "
            "(default: markup, docs, data, styles, SQL, etc.)"
        ),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.getenv("LANGUAGE_STATS_WORKERS", "4")),
        help="Concurrent repository downloads/cloc runs (default: 4)",
    )
    parser.add_argument(
        "--cloc-bin",
        default=os.getenv("LANGUAGE_STATS_CLOC_BIN", "cloc"),
        help="Path to the cloc executable (default: cloc)",
    )
    parser.add_argument(
        "--cloc-exclude-dirs",
        default=os.getenv("LANGUAGE_STATS_CLOC_EXCLUDE_DIRS", DEFAULT_CLOC_EXCLUDES),
        help="Comma-separated directory names excluded from cloc",
    )
    parser.add_argument(
        "--cloc-timeout",
        type=int,
        default=int(os.getenv("LANGUAGE_STATS_CLOC_TIMEOUT", "900")),
        help="Per-repository cloc timeout in seconds (default: 900)",
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
    if args.cloc_timeout < 1:
        print("错误：--cloc-timeout 必须大于等于 1。", file=sys.stderr)
        return 2

    configured_token = os.getenv("LANGUAGE_STATS_TOKEN")
    if (
        args.include_private
        and os.getenv("GITHUB_ACTIONS") == "true"
        and not configured_token
    ):
        print(
            "错误：统计私有仓库需要先在仓库 Actions Secret 中配置 "
            "LANGUAGE_STATS_TOKEN（只读 PAT）。",
            file=sys.stderr,
        )
        return 2
    token = configured_token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    exclude_repos = {
        item.strip() for item in args.exclude_repos.split(",") if item.strip()
    }
    exclude_languages = {
        item.strip() for item in args.exclude_languages.split(",") if item.strip()
    }
    try:
        rows, total_lines, repo_count, language_count, processed, _skipped = generate(
            username=username,
            token=token,
            output=args.output,
            include_private=args.include_private,
            include_forks=args.include_forks,
            include_archived=args.include_archived,
            exclude_repos=exclude_repos,
            exclude_languages=exclude_languages,
            max_languages=args.max_languages,
            title=args.title,
            workers=args.workers,
            cloc_bin=args.cloc_bin,
            cloc_exclude_dirs=args.cloc_exclude_dirs,
            cloc_timeout=args.cloc_timeout,
        )
    except (GitHubApiError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(
        f"已生成 {args.output}：{len(rows)} 行，{repo_count} 个仓库有代码，"
        f"共 {total_lines:,} 行、{language_count} 种语言；"
        f"扫描 {processed} 个仓库。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
