# 个人主页语言代码行数占比卡片

这是一个可直接放进 `username/username` 个人主页仓库的模板。它会扫描当前账号名下的**公开和私有仓库**（默认排除 fork），用 `cloc` 统计各编程语言的有效代码行数，生成仓库内的 `assets/languages.svg`，并显示占比最高的 10 门语言。

它不调用第三方图片服务，所以不会因为外部服务超时或失效而裂图。

## 统计口径

1. 列出当前账号自己的公开和私有仓库，默认排除 fork、包含 archived 仓库；
2. 下载每个仓库**默认分支**的 zipball；
3. 用 `cloc` 统计每门语言的有效代码行数：
   - 空行不计入；
   - 注释行不计入；
   - 只统计代码文件；
4. 通过 `data/programming_languages.txt` 白名单，只保留明确的编程/脚本语言；
5. 将保留下来的语言代码行数跨仓库累加；
6. 某语言占比 = 该语言累计行数 ÷ 所有保留语言累计行数；
7. 按占比从高到低显示前 10 门语言。

GitHub 自带的 `/languages` API 返回的是字节数，不是行数，所以这个版本会下载源码并用 `cloc` 实际数行。

### 为什么用白名单

`cloc` 会把 `C/C++ Header`、`Web Services Description`、`m4`、`SVG`、`Markdown`、`SQL`、`HTML`、`CSS` 等大量非编程文件类型也识别成“语言”。如果使用黑名单，就会不断漏掉新的奇怪分类。

因此现在反过来了：`data/programming_languages.txt` 里列出允许计入的 cloc 语言，不在文件里的分类全部忽略。运行时日志会打印被忽略的分类，方便判断是否需要补充。

如果发现某个真正的编程语言没被统计，在 `data/programming_languages.txt` 里加一行即可，例如：

```text
MyLanguage
```

白名单中的名称必须使用 `cloc --show-lang` 输出的语言名；匹配时大小写不敏感，但文字必须一致。`C#`、`F#` 这类包含 `#` 的名称也可以正常保存和匹配。可以用下面的命令查看当前 cloc 版本支持的准确名称：

```bash
cloc --show-lang
```

当前白名单已与 cloc 2.11 的 `--show-lang` 列表逐项比对，178 项全部匹配。卡片上的名称来自 cloc，和 GitHub Linguist 的显示名在主流语言上一致；少数 cloc 特有分类可能使用不同名称。如果看到真正的编程语言未收录，把它加进白名单即可。

## 排除某些仓库

`username/username` 个人主页仓库会**自动排除**，不需要手动配置。因为本模板的 Python 脚本也放在这个仓库里，如果不排除，主页仓库自己也会被算进语言占比。

有些仓库不是 fork，但也不是你自己写的。可以编辑：

```text
data/excluded_repos.txt
```

一行写一个仓库，两种写法都支持：

```text
old-project
lwlwilliam/old-project
```

以 `#` 开头的行是注释；匹配时忽略大小写。这个文件里的仓库不会参与语言统计。

也可以在 workflow 中用环境变量临时追加：

```yaml
LANGUAGE_STATS_EXCLUDE_REPOS: "repo-a,owner/repo-b"
```

### 卡片里的仓库数为什么可能比 GitHub 少

卡片副标题显示的是“扫描仓库数中，有多少个仓库有可统计的编程语言代码”，不是 GitHub 仓库总数。以下情况不会计入“有代码”的仓库：

- 仓库只有 README、Markdown、配置、HTML/CSS 等被白名单排除的内容；
- 仓库是空的，或没有默认分支；
- 仓库下载/分析失败；
- 仓库在 `data/excluded_repos.txt` 中被排除；
- `username/username` 个人主页仓库（自动排除）；
- fork（默认不统计）。

Actions 日志会输出总数：扫描了多少个、成功分析多少个、多少个有可统计代码、多少个没有代码、多少个失败。这样可以直接定位差异。

## 必须配置只读 Token

GitHub Actions 自动提供的 `GITHUB_TOKEN` 只对当前这个个人主页仓库有效，**不能读取你名下的其他私有仓库**。所以统计私有仓库必须额外提供一个只读 PAT。

推荐使用 **fine-grained personal access token**：

1. 打开 GitHub：
   `Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token`
2. 填写名称和过期时间；
3. **Resource owner** 选择你自己的账号；
4. **Repository access** 选择 **All repositories**；
5. **Repository permissions** 只勾选：
   - **Contents: Read-only**
   - **Metadata: Read-only**（通常会自动选中）
6. 不要授予任何写权限；
7. 生成并复制 token。

然后把 token 保存为 Actions Secret：

1. 打开你的个人主页仓库 `username/username`；
2. `Settings → Secrets and variables → Actions → New repository secret`；
3. Name 填：
   ```text
   LANGUAGE_STATS_TOKEN
   ```
4. Value 粘贴刚才的只读 token；
5. 保存。

Secret 不会进入代码、提交历史或生成的 SVG，GitHub Actions 日志也会自动打码。token 只读，建议设置过期时间并定期轮换。

> 如果不希望 Actions 读取私有仓库，可以把 workflow 中的 `LANGUAGE_STATS_INCLUDE_PRIVATE` 改成 `"false"`，并删除这个 Secret。

添加 Secret 后，手动运行一次 `Update language card`，或等下一次定时任务，也可以随便推一个 commit 触发。

## 创建与自动运行

1. 新建一个**公开**仓库，仓库名和你的 GitHub 用户名完全一致，例如用户名是 `alice`，仓库就是 `alice/alice`；
2. 把本目录的文件提交并推送到默认分支 `main`；
3. 按上面的步骤添加 `LANGUAGE_STATS_TOKEN`；
4. 推送到 `main` 后 workflow 会自动运行；以后每天 `03:17 UTC`（北京时间 `11:17`）自动更新。

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
| `LANGUAGE_STATS_INCLUDE_FORKS` | `false` | 是否统计 fork 仓库 |
| `LANGUAGE_STATS_INCLUDE_ARCHIVED` | `true` | 是否统计 archived 仓库 |
| `LANGUAGE_STATS_INCLUDE_PRIVATE` | `true` | 是否统计私有仓库；需要 `LANGUAGE_STATS_TOKEN` |
| `LANGUAGE_STATS_MAX_ROWS` | `10` | 显示占比最高的多少门语言 |
| `LANGUAGE_STATS_EXCLUDE_REPOS` | 空 | 额外用逗号分隔的排除列表，例如 `repo-a,owner/repo-b` |
| `LANGUAGE_STATS_EXCLUDE_REPOS_FILE` | `data/excluded_repos.txt` | 仓库排除文件路径 |
| `LANGUAGE_STATS_WORKERS` | `4` | 同时下载和分析的仓库数 |
| `LANGUAGE_STATS_CLOC_EXCLUDE_DIRS` | 常见依赖/构建目录 | 传给 `cloc --exclude-dir` 的目录名 |
| `LANGUAGE_STATS_CLOC_TIMEOUT` | `900` | 单个仓库的 `cloc` 超时秒数 |
| `LANGUAGE_STATS_TITLE` | `语言代码行数占比` | 卡片标题 |

语言白名单直接编辑：

```text
data/programming_languages.txt
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
GH_USERNAME=你的用户名 \
LANGUAGE_STATS_TOKEN=你的只读token \
python3 scripts/generate_language_stats.py
```

打开 `assets/languages.svg` 即可查看。

## 限制

- 只统计仓库默认分支的当前代码，不统计历史提交和其他分支；
- `cloc` 统计的是它识别出的源代码行数，不同语言规则可能与 GitHub Linguist 略有差异；
- 私有仓库越多、仓库越大，运行时间越长。workflow 已设置 120 分钟上限，并可调整 `LANGUAGE_STATS_WORKERS`；
- 如果需要排除某个仓库或某类目录，使用 `LANGUAGE_STATS_EXCLUDE_REPOS` 或 `LANGUAGE_STATS_CLOC_EXCLUDE_DIRS`。

## 文件说明

```text
.github/workflows/update-language-card.yml  # 定时生成并提交卡片
scripts/generate_language_stats.py          # 下载仓库、调用 cloc、渲染 SVG
data/programming_languages.txt              # 允许统计的编程语言白名单
data/excluded_repos.txt                     # 需要排除的仓库
assets/languages.svg                        # 自动生成的卡片
README.md                                   # 个人主页内容
```
