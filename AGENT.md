# AGENT.md - 微信公众号推送 Harness 规则契约

## 任务目标
追踪前沿 AI 技术动态，生成可验证、可归档、可发布到公众号的文章。

## 信息源
优先搜索以下来源：
- 官方博客：OpenAI、Anthropic、Google DeepMind 等。
- 技术社区：知乎、CSDN、掘金等。
- GitHub Trending/Search：AI 相关热门或近期更新仓库。
- 行业媒体：机器之心、量子位等。
- 微信公众号线索：Challenge Hub、量子位、AI学习的老章。

微信公众号线索通过搜狗微信搜索获取；只收录发布日期等于日报日期当天的文章。

## 关注领域
条目必须匹配至少一个领域：
- AI Agent / Agentic AI
- Harness Engineering / Context Engineering
- MCP (Model Context Protocol)
- LLM 推理与优化
- AI Coding / Vibe Coding
- 多模态大模型
- Codex / Claude Code / OpenAI / Anthropic

## 输出格式要求
基础日报输出为 `reports/YYYY-MM-DD.json`，结构如下：

```json
{
  "date": "YYYY-MM-DD",
  "topic_focus": "...",
  "items": [
    {
      "title": "中文标题",
      "source": "Blog | 社区 | GitHub | 媒体 | 微信公众号线索",
      "url": "https://...",
      "date": "YYYY-MM-DD",
      "category": "关注领域之一",
      "summary": "不超过 100 字",
      "insight": "不超过 150 字",
      "relevance": 1
    }
  ]
}
```

URL 必须真实；`relevance` 为 1-5，低于 3 的内容不要收录。

## 日报归档规则
- 默认归档到 `reports/YYYY-MM-DD.json`。
- 根目录 `report.json` 只作为手动指定输出时的临时文件。
- 同一 URL 不要重复收录。
- 默认最多收录 10 条；`agent.py --limit N` 只控制最终写入数量，不限制前置抓取。

## 深度阅读中间文件规则
深度阅读文件为 `reports/YYYY-MM-DD.deepread.json`，用于从基础日报中选择 3-5 条内容生成公众号文章。它不是原文全文归档，不应大段保存或搬运原文内容。

选择优先级：
- 相关度：贴合 AI Agent、MCP、Coding Agent、Context Engineering、Harness Engineering、LLM 等。
- 可写性：有故事线、冲突点、技术启发或工程判断。
- 组合价值：能形成“大事件 + 工具 + 趋势 + 案例”的组合更优。
- 可访问性：正文和标题可抓取；抓不到正文的内容降级或放弃。

结构必须与 `verify_deepread.py` 和 `pipeline.py` 一致：

```json
{
  "date": "YYYY-MM-DD",
  "source_report": "reports/YYYY-MM-DD.json",
  "generation_rule": "生成规则说明",
  "selection_criteria": {
    "relevance": "相关度选择标准",
    "writeability": "可写性选择标准",
    "composition": "组合价值选择标准",
    "source_access": "来源可访问性选择标准"
  },
  "selected_items": [
    {
      "title": "...",
      "url": "https://...",
      "source": "...",
      "article_type": "主线型 | 解读型 | 工具型",
      "output_file": "daily_paper/YYYY-MM-DD-文章标题.md",
      "raw_text_status": "fetched | partial | failed",
      "selection_reason": "选择理由",
      "article_plan": {
        "title": "最终 Markdown 标题",
        "core_claim": "文章核心判断"
      }
    }
  ]
}
```

`selected_items` 必须为 3-5 条；每条必须指向一篇独立的 `daily_paper/*.md`，不能共用同一个 `output_file`。

## 公众号推文编辑规则
每条深挖新闻生成一篇独立 Markdown，保存为 `daily_paper/YYYY-MM-DD-文章标题.md`。不要把多条新闻合并成一篇总述。

文章只使用三种形态：
- 主线型：围绕一条大新闻展开，强调发生了什么、为什么重要、影响和后续观察。
- 解读型：围绕趋势展开，强调出现原因、技术含义和行业影响。
- 工具型：围绕 GitHub 或产品工具展开，强调它是什么、适合谁、技术看点和局限。

正文要求：
- 最终 Markdown 必须原创重写，不能搬运原文。
- 不要出现“参考来源”“来源”“据某媒体报道”等来源归因区块或表述。
- 不要把来源限制误扩大为禁止公司名、产品名、模型名或项目名；它们是文章主体或关键事实时可以自然出现。
- 工具型文章如果介绍 GitHub 或开源项目，应在开头紧跟标题给出项目链接。
- 段落优先使用 2-4 句话组成自然段，避免大量连续超短段落。
- 二级标题不要直接写成“发生了什么”“为什么重要”“技术看点”“适合谁看”等功能标签，应写成具体判断。

## 显式 Pipeline 入口
完整流程由 `pipeline.py` 触发。每次启动必须读取本文件作为规则契约：

```bash
python pipeline.py --date YYYY-MM-DD
```

默认流程：

```text
读取 AGENT.md
-> 运行 agent.py 生成 reports/YYYY-MM-DD.json
-> 运行 verify.py 验证基础日报
-> 生成或复用 reports/YYYY-MM-DD.deepread.json
-> 生成或复用 daily_paper/ 下的 3-5 篇 Markdown
-> 运行 verify_deepread.py
-> 逐篇运行 verify_article.py
```

常用参数：

```bash
python pipeline.py --date 2026-05-26 --skip-fetch
python pipeline.py --date 2026-05-26 --refresh-deepread --overwrite-articles
python pipeline.py --date 2026-05-26 --article-count 5
```

## 运行问题沉淀规则
如果 pipeline 任一阶段失败，`pipeline.py` 将失败命令、退出码、输出和建议沉淀到：

```text
errors/YYYY-MM-DD-log.md
```

错误日志只作为待确认清单，不能自动写回 `AGENT.md`。人工确认后，才把稳定规则整理进本文件。

## 编辑层验证规则
基础日报使用 `verify.py`；深度阅读文件使用 `verify_deepread.py`；最终文章使用 `verify_article.py`。

`verify_deepread.py` 必须校验：
- JSON 合法。
- 顶层字段 `date`、`source_report`、`selected_items` 存在。
- `selected_items` 数量为 3-5 条。
- 每条包含标题、URL、来源、文章类型、输出文件、抓取状态、选择原因和成稿计划。
- `article_type` 只能是 `主线型`、`解读型`、`工具型`。
- 每条指向真实存在且互不重复的 `daily_paper/*.md`。

`verify_article.py` 必须校验：
- 文件名符合 `YYYY-MM-DD-文章标题.md`。
- Markdown 以一级标题开头，且只有一个一级标题。
- 至少包含 3 个二级标题。
- 正文篇幅不能过短。
- 正文不能出现参考来源区块、URL、媒体名、公众号名或平台来源名。
- 文章类型结构完整。
- 编辑评分不低于 75。

如果编辑评分低于 75，当前成稿不得进入发布态；应根据 `rewrite_suggestions` 修改对应 Markdown 后重新验证。

运行方式：

```bash
python verify_deepread.py reports/YYYY-MM-DD.deepread.json
python verify_article.py daily_paper --deepread reports/YYYY-MM-DD.deepread.json --verbose
```

## 已知陷阱
- 不要收录纯营销或广告内容。
- 不要编造 URL。
- 搜狗微信搜索结果必须解析发布时间；无发布时间或不是当天的文章不要收录。
- 输出必须是合法 JSON。
