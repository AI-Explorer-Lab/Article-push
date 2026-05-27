# AGENT.md - 微信公众号推送 Harness 规则契约

## 任务目标
追踪前沿 AI 技术动态，生成可验证、可归档、可发布到公众号的文章。

## Harness 架构契约
本项目把 harness 视为模型与外部环境之间的运行基底，而不是单个提示词。它的目标是让公众号内容生产链路做到：看得准、做得对、错了能兜底。

本项目的 harness 拆成三组六层：

| 组 | 层 | 本项目要解决的问题 | 落地位置 |
| --- | --- | --- | --- |
| 输入侧 | 上下文精细化 | 当前这轮该让模型看到什么信息源、证据和约束？ | `AGENT.md`、`reports/YYYY-MM-DD.json`、`reports/YYYY-MM-DD.deepread.json` |
| 动作侧 | 工具系统 | 模型可以通过哪些脚本抓取、生成、校验和记录？ | `agent.py`、`pipeline.py`、`verify.py`、`verify_deepread.py`、`verify_article.py` |
| 动作侧 | 执行编排 | 下一步该跑抓取、日报校验、deepread、文章生成还是文章校验？ | `pipeline.py` |
| 输入侧 | 记忆与状态 | 跨阶段、跨文章、跨天分别该记住什么？ | `states/YYYY-MM-DD.article_states.json`、`errors/YYYY-MM-DD-log.md`、`AGENT.md` |
| 校验侧 | 评估与观测 | 怎样知道结果好不好、哪一步出了问题？ | 三个验证脚本、阶段日志、文章评分 |
| 校验侧 | 约束与恢复 | 出错后如何停下、归因、重跑，而不是把坏结果发布？ | `errors/`、`--skip-fetch`、`--refresh-deepread`、`--overwrite-articles` |

六层的项目规则如下：
- 上下文精细化：基础日报只收录真实 URL、有效日期、相关度不低于 3 的条目；deepread 只保留 3-5 条可写、可验证的候选，不保存原文全文。
- 工具系统：每类动作只通过明确脚本完成，抓取集中在 `agent.py`，编排集中在 `pipeline.py`，质量门槛集中在验证脚本；不要把抓取、写作、验证规则散落到临时手工步骤里。
- 执行编排：完整轨道固定为“读取规则 -> 抓取日报 -> 校验日报 -> 生成/复用 deepread -> 逐篇生成文章 -> 校验 deepread -> 逐篇校验文章 -> 更新状态”。
- 记忆与状态：单篇原文上下文只在当前文章内使用，写完即丢弃；当天进度写入 `states/`；跨天稳定经验只有人工确认后才进入 `AGENT.md`。
- 评估与观测：每个阶段必须留下命令、返回码和关键输出；文章必须通过 `verify_article.py`，评分低于 75 不得进入发布态。
- 约束与恢复：失败时 pipeline 只写错误日志和建议沉淀规则，不自动修改长期规则；允许用 `--skip-fetch` 复用已验证日报，用 `--refresh-deepread` 重建选择，用 `--overwrite-articles` 重写文章。

每次运行都要形成可审计 episode：基础日报、deepread 选择文件、逐篇 Markdown、短期状态、失败日志（如有）和验证输出。判断 pipeline 是否完成，不看是否生成了文本，而看这些证据是否能证明“来源真实、选择有据、文章独立、验证通过、失败可归因”。

## 信息源
优先搜索以下来源：
- 官方博客：OpenAI、Anthropic、Google DeepMind 等。
- 技术社区：知乎、CSDN、掘金等。
- GitHub Trending/Search：AI 相关热门或近期更新仓库。
- 行业媒体：机器之心等。
- 微信公众号线索：Challenge Hub、量子位、AI学习的老章。

微信公众号线索通过搜狗微信搜索获取；只收录发布日期等于日报日期当天的文章。
量子位只允许使用微信公众号线索，不使用 QbitAI 官网、WordPress JSON、网页镜像或其他网页转载作为量子位来源。若搜狗微信无法给出当天公众号推送时间，则该条量子位内容直接放弃。

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

生成文章时必须逐篇处理：
- 先读取当前条目的原文正文或可访问正文。
- 只在当前条目的原文事实基础上改写，不允许只根据标题发挥。
- 当前文章写完并验证后，丢弃该篇原文上下文，再处理下一篇。
- 不同文章之间不能复用上一篇的事实、表达套路或判断框架。

LLM 写作层规则：
- 默认规则写作器可继续使用；启用 `pipeline.py --use-llm-writer` 时，最终成稿交给 LLM 改写。
- LLM 必须基于当前条目的原文全文写作，不允许只基于关键词、摘要、`must_include` 或 deepread 计划发挥。
- 原文全文只允许存在于单篇文章生成函数的局部上下文中；不得写入 `reports/`、`states/`、`errors/` 或 deepread 文件。
- 写完当前文章并完成校验后，必须丢弃原文全文，再处理下一篇。
- 如果原文全文抓取失败，LLM 写作层必须失败并写入错误日志，不得退化为关键词写作。
- 如果原文全文超过模型上下文或 `--llm-max-original-chars`，必须失败并写入错误日志，不得自动节选。
- LLM 初稿未通过 `verify_article.py` 时，可以把“原文全文 + 当前文章 + 校验错误”交给 LLM 修订，默认最多修订 1 次；仍失败则不得发布。
- LLM 调用使用 OpenAI-compatible Chat Completions 接口；运行前需要配置 `LLM_API_KEY` 或 `OPENAI_API_KEY`，并通过 `--llm-model` 或 `LLM_MODEL` 指定模型；`LLM_API_BASE` 可选。

文章只使用三种形态：
- 主线型：围绕一条大新闻展开，事实密度优先。先讲清楚原文发生了什么、谁做了什么，再解释为什么重要、影响和后续观察。
- 解读型：围绕趋势展开，原文事实是证据。强调出现原因、技术含义和行业影响，但不能把单条新闻过度拔高成确定趋势。
- 工具型：围绕 GitHub 或产品工具展开，少写宣传词。强调它是什么、适合谁、技术看点、失败路径和局限。

文章风格由类型契约控制，而不是由固定正文模板控制：
- 主线型必须回答：发生了什么、谁做了什么、为什么值得单独写、后续应该看什么。
- 解读型必须回答：趋势是什么、原文事实如何支撑趋势、技术含义是什么、对行业或开发者有什么影响。
- 工具型必须回答：它是什么、适合谁、技术看点是什么、局限和验证点是什么。
- 二级标题可以按类型承担结构功能，但标题文字必须写成具体判断，不能直接暴露“发生了什么”“技术看点”等功能标签。

文章风格偏好：
- 开头直接进入具体对象、动作和矛盾，不写“这条动态值得拆开看”“真正值得看的是……”这类套话。
- 观点要像技术编辑做判断：先给事实，再给判断，再说明边界；不要用反复的反转句制造口号感。
- 少写宏大行业判断，多写具体流程、工具调用、上下文、状态、权限、验证、失败恢复等工程细节。
- 每篇文章要有自己的表达节奏；禁止复用同一组开头、过渡、结尾和标题句式。
- 结尾给出可观察的后续指标，不强行升华，不用“所以最后落回一个简单问题”这类模板收束。

三种文章的写法：
- 主线型：按“主体动作 -> 流程变化 -> 影响对象 -> 后续观察”推进，语气像在复盘一件具体工程事件。
- 解读型：按“信号边界 -> 技术机制 -> 开发者感知 -> 落地条件”推进，避免把单条新闻写成确定趋势。
- 工具型：按“项目定位 -> 适用任务 -> 接入成本 -> 失败边界”推进，必须写清谁适合用、谁不适合用。

正文要求：
- 最终 Markdown 必须原创重写，不能搬运原文。
- 正文必须覆盖 deepread `article_plan.must_include` 中的关键对象；没有覆盖足够关键对象的文章不能发布。
- 不要出现“参考来源”“来源”“据某媒体报道”等来源归因区块或表述。
- 不要把来源限制误扩大为禁止公司名、产品名、模型名或项目名；它们是文章主体或关键事实时可以自然出现。
- 工具型文章如果介绍 GitHub 或开源项目，应在开头紧跟标题给出项目链接。
- 段落优先使用 2-4 句话组成自然段，避免大量连续超短段落。
- 二级标题不要直接写成“发生了什么”“为什么重要”“技术看点”“适合谁看”等功能标签，应写成具体判断。
- 禁止复用旧模板腔，例如“真正值得看的，不是标题里那一点热闹”“真正考验的是落地边界”等泛化表达。

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
-> 更新 states/YYYY-MM-DD.article_states.json，形成当次 episode trace
```

## 短期状态规则
当天 pipeline 运行状态写入 `states/YYYY-MM-DD.article_states.json`。

状态文件只记录当天短期状态，不作为跨天长期记忆：
- 每篇文章的处理阶段。
- 原文抓取状态。
- 单篇上下文范围。
- 本篇上下文是否已丢弃。
- 成稿字数、关键对象数量和验证分数。

状态文件不得保存原文全文，也不得把某篇文章的事实迁移到下一篇。跨天稳定经验只允许人工确认后写入 `AGENT.md`。

常用参数：

```bash
python pipeline.py --date 2026-05-26 --skip-fetch
python pipeline.py --date 2026-05-26 --refresh-deepread --overwrite-articles
python pipeline.py --date 2026-05-26 --article-count 5
python pipeline.py --date 2026-05-26 --use-llm-writer --overwrite-articles
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
