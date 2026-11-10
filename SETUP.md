# 个人主页语言代码行数占比卡片

这是一个可直接放进 `username/username` 个人主页仓库的模板。它会扫描当前账号名下的**公开仓库**，用 `cloc` 统计各编程语言的有效代码行数，生成仓库内的 `assets/languages.svg`，并显示占比最高的 10 门语言。

它不调用第三方图片服务，所以不会因为外部服务超时或失效而裂图。

## 统计口径

1. 列出当前账号自己的**公开仓库**，默认包含 archived、fork 仓库；
2. 下载每个仓库**默认分支**的 zipball；
3. 用 `cloc` 统计每门语言的有效代码行数：
   - 空行不计入；
   - 注释行不计入；
   - 只统计 `cloc` 能识别的代码文件；
4. 排除 Markdown、HTML、CSS、SQL、YAML、JSON 等非编程语言；
5. 将保留下来的语言代码行数跨仓库累加；
6. 某语言占比 = 该语言累计行数 ÷ 所有保留语言累计行数；
7. 按占比从高到低显示前 10 门语言。

GitHub 自带的 `/languages` API 返回的是字节数，不是行数，所以这个版本会下载源码并用 `cloc` 实际数行。

默认排除的 `cloc` 语言：

```text
Markdown, HTML, CSS, SCSS, Sass, Less, XML, YAML, JSON, TOML, INI,
SQL, PL/SQL, T-SQL, TeX, reStructuredText, AsciiDoc, Text,
Jupyter Notebook, GraphQL, XSLT, DTD, CSV,
Makefile, Dockerfile, CMake, Meson, HCL, Protocol Buffers
```

默认还会排除常见依赖和构建目录：

```text
.git, node_modules, vendor, dist, build, target, coverage,
.venv, venv, __pycache__, Pods, .next, .nuxt
```

## 不需要配置任何 Token

工作流使用 GitHub Actions 自动提供的临时 `GITHUB_TOKEN`：

```yaml
permissions:
  contents: write
```

它只在本次运行中有效，不需要创建 Secret，也不会进入代码或 Git 历史。公开仓库的读取和卡片提交都由这个自动 token 完成。

> 如果以后要统计私有仓库，需要额外添加只读 PAT，并修改 workflow 中的 `LANGUAGE_STATS_INCLUDE_PRIVATE`。本模板默认不读取私有仓库。

## 创建与自动运行

1. 新建一个**公开**仓库，仓库名和你的 GitHub 用户名完全一致，例如用户名是 `alice`，仓库就是 `alice/alice`；
2. 把本目录的文件提交并推送到默认分支 `main`；
3. 推送到 `main` 后，`Update language card` 会自动运行一次；
4. 之后每天 `03:17 UTC`（北京时间 `11:17`）自动运行，不需要手动点 Actions。

```yaml
on:
  schedule:
    - cron: "17 3 * * *"
  push:
    branches: [main]
```

`workflow_dispatch` 只是保留给调试用。GitHub 的高峰期可能让定时任务延迟几十分钟；公开仓库如果连续 60 天没有任何提交，GitHub 也可能自动停用定时工作流，此时随便提交一次即可重新激活。

如果 Actions 无法 push SVG，请在 `Settings → Actions → General → Workflow permissions` 中选择 **Read and write permissions**。

## 可选配置

直接修改 `.github/workflows/update-language-card.yml` 里的环境变量即可：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LANGUAGE_STATS_INCLUDE_FORKS` | `true` | 是否统计 fork 仓库 |
| `LANGUAGE_STATS_INCLUDE_ARCHIVED` | `true` | 是否统计 archived 仓库 |
| `LANGUAGE_STATS_INCLUDE_PRIVATE` | `false` | 是否统计私有仓库；开启后需要只读 PAT |
| `LANGUAGE_STATS_MAX_ROWS` | `10` | 显示占比最高的多少门语言 |
| `LANGUAGE_STATS_EXCLUDE_REPOS` | 空 | 逗号分隔的排除列表，例如 `repo-a,owner/repo-b` |
| `LANGUAGE_STATS_EXCLUDE_LANGUAGES` | 见上文 | 要排除的 `cloc` 语言名，大小写不敏感 |
| `LANGUAGE_STATS_WORKERS` | `4` | 同时下载和分析的仓库数 |
| `LANGUAGE_STATS_CLOC_EXCLUDE_DIRS` | 常见依赖/构建目录 | 传给 `cloc --exclude-dir` 的目录名 |
| `LANGUAGE_STATS_CLOC_TIMEOUT` | `900` | 单个仓库的 `cloc` 超时秒数 |
| `LANGUAGE_STATS_TITLE` | `语言代码行数占比` | 卡片标题 |

例如只想排除 SQL、HTML、CSS、Markdown：

```yaml
LANGUAGE_STATS_EXCLUDE_LANGUAGES: "SQL,HTML,CSS,Markdown"
```

## 本地预览

先安装 `cloc`：

```bash
# macOS
brew install cloc

# Ubuntu / Debian
sudo apt-get install cloc
```

然后运行：

```bash
GH_USERNAME=你的用户名 python3 scripts/generate_language_stats.py
```

匿名 API 限额较低，仓库多时可以在本地设置 `GITHUB_TOKEN` 提高限额。打开 `assets/languages.svg` 即可查看。

## 限制

- 只统计公开仓库默认分支的当前代码，不统计私有仓库、历史提交和其他分支；
- `cloc` 统计的是它识别出的源代码行数，不同语言规则可能与 GitHub Linguist 略有差异；
- 仓库很大或公开仓库很多时，运行时间会明显增加。workflow 已设置 120 分钟上限，并可调整 `LANGUAGE_STATS_WORKERS`；
- 如需排除某个仓库或某类目录，使用 `LANGUAGE_STATS_EXCLUDE_REPOS` 或 `LANGUAGE_STATS_CLOC_EXCLUDE_DIRS`。

## 文件说明

```text
.github/workflows/update-language-card.yml  # 定时生成并提交卡片
scripts/generate_language_stats.py          # 下载仓库、调用 cloc、渲染 SVG
assets/languages.svg                        # 自动生成的卡片
README.md                                   # 个人主页内容
```
