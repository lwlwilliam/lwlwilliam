# 个人主页语言占比卡片

这是一个可直接放进 `username/username` 个人主页仓库的模板。它会先过滤掉每个仓库中代码占比不超过 10% 的小语言，再把保留下来的语言按代码字节数汇总，计算每种语言在全部入选代码中的总占比，生成仓库内的 `assets/languages.svg`。

它不调用 github-readme-stats 之类的第三方图片服务，因此不会因为外部服务超时、限流或域名失效而裂图。

## 统计口径

对每个仓库分别执行：

1. 用 GitHub Languages API 返回的语言字节数得到该仓库的代码总量；
2. 只保留在该仓库内代码占比**严格超过 10%** 的语言；
3. 把这些入选语言的原始字节数跨仓库累加；
4. 某语言占比 = 该语言累计字节数 ÷ 所有入选语言累计字节数。

这样不是简单地“每个仓库前两名各算一次”，而是按实际代码量加权；同时通过 10% 阈值排除配置文件、脚本、生成代码之类的小语言噪声。

例如：

- 仓库 A：Python 7000 字节（70%）、Shell 2000（20%）、HTML 1000（10%）
- 仓库 B：TypeScript 8000（80%）、Python 1000（10%）、CSS 1000（10%）

严格超过 10% 的入选语言是 Python、Shell、TypeScript，累计 17000 字节：

- Python：7000/17000 = 41.2%
- TypeScript：8000/17000 = 47.1%
- Shell：2000/17000 = 11.8%

如果某个仓库没有任何语言超过 10%，这个仓库不会被计入卡片统计。

默认只统计账号自己拥有的**公开仓库**，且默认排除 fork；archived 仓库默认计入。没有语言数据的空仓库会被跳过。

## 创建步骤

1. 新建一个**公开**仓库，仓库名和你的 GitHub 用户名完全一致，例如用户名是 `alice`，仓库就是 `alice/alice`。
2. 把本目录的文件提交并推送到默认分支 `main`。
3. 推送后，`Update language card` 会自动运行一次；之后每天 `03:17 UTC`（北京时间 `11:17`）自动运行，不需要手动点 Actions。
4. 等 Actions 提交 `assets/languages.svg` 后，主页 README 中的图片就会正常显示。

定时任务写在 `.github/workflows/update-language-card.yml`：

```yaml
on:
  schedule:
    - cron: "17 3 * * *"
  push:
    branches: [main]
```

`workflow_dispatch` 只是保留给调试用，日常不需要操作。GitHub 的高峰期可能让定时任务延迟几十分钟；公共仓库如果连续 60 天没有任何提交，GitHub 也可能自动停用定时工作流，此时随便提交一次或手动运行一次即可重新激活。

不需要在仓库中添加任何 PAT 或 Secret。工作流使用 GitHub 自动提供的临时 `GITHUB_TOKEN`：

```yaml
permissions:
  contents: write
```

它只用于读取公开仓库信息和提交生成后的 SVG。

> 如果 Actions 无法 push，请在 `Settings → Actions → General → Workflow permissions` 中选择 **Read and write permissions**。如果默认分支有分支保护规则，也需要允许 GitHub Actions 写入。

## 可选配置

直接修改 `.github/workflows/update-language-card.yml` 里的环境变量即可：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LANGUAGE_STATS_INCLUDE_FORKS` | `false` | 是否统计 fork 仓库 |
| `LANGUAGE_STATS_INCLUDE_ARCHIVED` | `true` | 是否统计 archived 仓库 |
| `LANGUAGE_STATS_INCLUDE_PRIVATE` | `false` | 是否统计私有仓库；开启后需要额外 token |
| `LANGUAGE_STATS_MIN_REPO_SHARE` | `10` | 单个仓库内代码占比严格超过该百分比的语言才计入 |
| `LANGUAGE_STATS_MAX_ROWS` | `9` | 最多显示多少行，超出部分合并为“其他” |
| `LANGUAGE_STATS_EXCLUDE_REPOS` | 空 | 以逗号分隔的排除列表，例如 `repo-a,owner/repo-b` |
| `LANGUAGE_STATS_TITLE` | `语言使用占比` | 卡片标题 |

### 如果确实要统计私有仓库

默认方案不读取私有仓库，因此不需要任何敏感信息。若你接受使用一个只读 token，可以：

1. 创建一个 fine-grained PAT，只授予你自己的仓库 **Metadata: Read-only** 和 **Contents: Read-only** 权限；
2. 在仓库的 `Settings → Secrets and variables → Actions` 中添加名为 `LANGUAGE_STATS_TOKEN` 的 secret；
3. 把工作流中的 `LANGUAGE_STATS_INCLUDE_PRIVATE` 改为 `"true"`，并把 `GITHUB_TOKEN` 那一行改成：

```yaml
LANGUAGE_STATS_TOKEN: ${{ secrets.LANGUAGE_STATS_TOKEN }}
```

token 不会进入 Git 历史或生成的文件，但如果你不需要私有仓库数据，建议保持默认设置。

## 本地预览

```bash
GH_USERNAME=你的用户名 \
GITHUB_TOKEN="$(gh auth token)" \
python3 scripts/generate_language_stats.py
```

然后打开 `assets/languages.svg`。如果不传 token，GitHub 匿名 API 限额较低，仓库多时可能失败。

## 文件说明

```text
.github/workflows/update-language-card.yml  # 定时生成并提交卡片
scripts/generate_language_stats.py         # 统计与 SVG 渲染，仅用 Python 标准库
assets/languages.svg                       # 自动生成的卡片
README.md                                  # 个人主页内容
```
