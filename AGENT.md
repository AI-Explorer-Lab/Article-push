# AGENT.md - 前沿 AI 技术研究 Agent 工作手册

## 任务目标
自动追踪前沿 AI 技术动态，生成结构化的每日技术简报。

## 信息源
通过网络搜索以下渠道的最新内容：
1. 技术博客：OpenAI、Anthropic、Google DeepMind 等官方博客。
2. 技术社区：知乎、CSDN、掘金等平台的 AI 热门文章。
3. GitHub Trending/Search：AI 相关的热门或近期更新仓库。
4. 行业媒体：机器之心、量子位等。
5. 微信公众号线索：Challenge Hub、量子位、AI学习的老章。

公众号线索通过搜狗微信搜索获取；只收录发布日期等于日报日期当天的公众号文章。

## 关注领域
条目必须匹配至少一个关注领域：
- AI Agent / Agentic AI
- Harness Engineering / Context Engineering
- MCP (Model Context Protocol)
- LLM 推理与优化
- AI Coding / Vibe Coding
- 多模态大模型
- Codex / Claude Code / OpenAI / Anthropic

## 输出格式要求
生成 JSON 文件，结构如下：
- date: 日期，格式为 YYYY-MM-DD。
- topic_focus: 本期关注主题。
- items: 条目数组，每条包含：
  - title: 中文标题。
  - source: 来源，例如 Blog、社区、GitHub、媒体、微信公众号线索。
  - url: 原文链接，必须是真实可访问的 URL。
  - date: 发布日期，格式为 YYYY-MM-DD。
  - category: 所属领域，从关注领域中选择。
  - summary: 一句话摘要，不超过 100 字。
  - insight: 技术洞察，不超过 150 字。
  - relevance: 相关度评分，1-5，最高 5。

## 日报归档规则
- 每天生成的日报 JSON 统一保存到 `reports/` 文件夹。
- 文件名使用当天日期，格式为 `YYYY-MM-DD.json`。
- 示例：`reports/2026-05-26.json`。
- 根目录 `report.json` 只作为手动指定输出时的临时文件，不作为默认归档位置。

## 收录数量规则
- 默认每期最多收录 10 条。
- 可通过 `agent.py --limit N` 临时调整最终收录数量。
- `--limit` 只控制最终写入日报的条目数，不限制前置抓取和候选过滤数量。

## 深度阅读中间文件规则
在基础日报 JSON 之后，允许增加一个深度阅读中间文件，用于把 10 条候选内容进一步转化为公众号写作素材。

- 中间文件建议保存为 `reports/YYYY-MM-DD.deepread.json`。
- 中间文件不是原文全文归档，不应大段保存或搬运原文内容。
- 原文抓取只用于理解、抽取事实、生成摘要、形成技术判断和保留引用链接。
- 最终公众号推文必须是原创重写，并根据原文的风格写不同的内容。
- 中间文件应记录每条深挖内容的选择原因、文章角色、正文抓取状态、核心事实、技术启发、改写备注和引用信息。

从 10 条日报候选中选择深挖条目时，优先使用以下维度：

- 相关度：是否高度贴合 AI Agent、MCP、Coding Agent、Context Engineering、Harness Engineering、LLM 等 AI 领域知识。
- 可写性：是否适合改写成公众号内容，是否有故事线、冲突点、技术启发。
- 组合价值：如果包含 GitHub 项目，优先形成“大事件 + 工具 + 技术趋势 + 案例”的组合。
- 可访问性：原文能否抓到正文和标题；无法抓到正文的内容应降级为辅助线索或放弃深挖。

中间文件中的深挖条目可参考以下结构：

```json
{
  "title": "...",
  "url": "...",
  "source": "...",
  "selected": true,
  "selection_reason": "选择这条内容的编辑理由",
  "article_role": "主线核心 | 技术趋势证据 | 工具案例 | 快讯补充",
  "raw_text_status": "fetched | partial | failed",
  "extracted_facts": [
    "可验证事实 1",
    "可验证事实 2"
  ],
  "technical_takeaways": [
    "技术启发 1",
    "工程启发 2"
  ],
  "rewrite_notes": "面向公众号读者的改写角度",
  "citation": {
    "title": "...",
    "url": "..."
  }
}
```

## 公众号推文编辑规则
日报 JSON 只回答“今天有哪些候选信息”，深度阅读中间文件负责从候选中选出若干条值得深挖的新闻。最终公众号稿件应按“每条深挖新闻生成一篇 Markdown 文章”的方式产出，而不是把多条新闻强行合并成一篇总述。

每篇文章都应先围绕对应新闻单独判断文章形态、风格，再组织重写语言。

最终 Markdown 正文不要出现参考文章的来源和名字，例如量子位、机器之心等。可以在中间文件中保留 `citation`、`url`、`source` 等字段用于事实追踪和内部审查，但不要在最终 Markdown 里写“参考来源”“来源”“据某媒体报道”等区块或表述。

不要把“参考来源名”误扩大为“所有公司名、产品名、项目名都不能出现”。如果某家公司、模型、产品或开源项目本身就是文章主体或关键事实，应在正文中自然出现，例如 Anthropic、DeepMind、Claude、SDK、MCP、具体项目名等。禁止的是把它们作为“我参考了谁的文章/谁报道了这件事”的来源归因来写。

工具型文章如果介绍 GitHub 或其他开源项目，应在文章开头紧跟标题给出项目链接，便于读者直接访问。推荐格式为：

```markdown
# 文章标题

`[点击跳转项目](<https://github.com/owner/repo>)`

正文开始……
```

项目链接属于文章主体信息，不属于“参考来源”区块，不应放到文末作为资料列表。

公众号正文的段落要有阅读节奏，但不要把每一两句话都强行拆成独立段落。优先使用 2-4 句话组成一个自然段；只有用于强调、转折、制造停顿的句子才单独成段。整体上应避免大量连续的超短段落，否则文章会显得像提纲、口播稿或 AI 生成草稿，而不是成熟的公众号文章。

只保留以下三种文章形态：

1. 主线型：有一条明显大新闻，其他内容都围绕它服务，适合“一个核心观点 + 多条证据”。
2. 解读型：某个技术趋势连续出现，适合“为什么大家都在做 X”。
3. 工具型：GitHub 或产品工具多，适合“今天值得关注的 1 个 Agent 工具”。

不同类别的写法应有明显差异：

- 主线型：开头直接抛出大新闻和判断，正文围绕“发生了什么、为什么重要、它改变了什么、后续看什么”展开。
- 解读型：开头提出趋势问题，正文围绕“为什么出现、背后原因、技术含义、对开发者/行业的影响”展开。
- 工具型：开头直接说明这个工具解决什么问题，正文围绕“它是什么、适合谁、技术看点、局限和使用场景”展开。

上述结构是写作时的内在组织，不应作为二级标题的裸露模板。最终 Markdown 的二级标题不要直接写成“发生了什么”“为什么重要”“技术看点”“适合谁看”“接下来该看什么”等功能标签，也不要使用“发生了什么：XXX”“为什么重要：XXX”这类格式。标题应把结构意图融入具体判断中，例如用“难点不在答案，而在探索路径”代替“发生了什么”，用“真正的门槛是稳定推进未知问题”代替“接下来该看什么”。

标题应比日报标题更有吸引力，优先表达判断、冲突或趋势，不要写成平铺直叙的新闻列表。例如：

- `AI Agent 的下一步，已经不是“会聊天”了`
- `这次更新，暴露了 Coding Agent 真正的门槛`
- `为什么所有人都在把 Agent 往“工程化”推？`
- `Context Engineering，可能是 Agent 落地的隐藏主线`
- `今天看到一个小项目，戳中了 Agent 落地的大问题`

推荐文件流如下：

1. `reports/YYYY-MM-DD.json`：基础 10 条候选。
2. `reports/YYYY-MM-DD.deepread.json`：自动选择深挖条目、原文素材卡、每条新闻的推荐文章形态和成稿计划。
3. `daily_paper/YYYY-MM-DD-文章标题.md`：最终生成的公众号 Markdown 文章；每条深挖新闻对应一篇 Markdown。
4. 可选 `reports/YYYY-MM-DD.article.meta.json`：标题候选、文章类型、引用来源、事实核查状态、编辑备注。

最终 Markdown 文章统一保存到 `daily_paper/` 文件夹，文件名格式为 `YYYY-MM-DD-文章标题.md`。文章标题用于文件名时应去除或替换不适合文件系统的字符，并保持可读性。同一天生成多篇文章时，文件名通过不同文章标题区分，不要把多条新闻合并到同一个 Markdown 文件中。

## 显式 Pipeline 入口

项目的完整流程由 `pipeline.py` 触发。它把本文件作为规则契约读取，然后按顺序执行：

```bash
python pipeline.py --date YYYY-MM-DD
```

默认流程为：

```text
读取 AGENT.md 规则契约
-> 运行 agent.py 生成 reports/YYYY-MM-DD.json
-> 运行 verify.py 验证基础日报
-> 生成或复用 reports/YYYY-MM-DD.deepread.json
-> 生成或复用 daily_paper/ 下的 3-5 篇 Markdown 成稿，默认 5 篇
-> 运行 verify_deepread.py 验证深读计划
-> 运行 verify_article.py 验证最终成稿
```

常用参数：

```bash
python pipeline.py --date 2026-05-26 --skip-fetch
python pipeline.py --date 2026-05-26 --refresh-deepread --overwrite-articles
python pipeline.py --date 2026-05-26 --article-count 5
```

`--skip-fetch` 用于复用已有 `reports/YYYY-MM-DD.json`，避免重复抓取外部来源。
`--refresh-deepread` 会根据基础日报重新生成 deepread 文件。
`--overwrite-articles` 会覆盖已有 Markdown 成稿。

## 运行问题沉淀规则

如果 pipeline 任一阶段失败，`pipeline.py` 会把失败命令、退出码、输出和建议沉淀规则写入：

```text
errors/YYYY-MM-DD-log.md
```

这些规则只作为待确认清单保存，不能自动写回 `AGENT.md`。需要人工确认后，再把确认过的规则整理进本文件。

推荐处理方式：

```text
验证失败
-> 查看 errors/YYYY-MM-DD-log.md
-> 勾选或修改建议规则
-> 人工确认后写入 AGENT.md
-> 重新运行 pipeline.py
```

整体流程保持 harness 基础功能不变，只在日报生成之后追加编辑层：

```text
抓取 10 条候选
→ 打分去重，生成日报 JSON
→ 按相关度、可写性、组合价值、正文可抓取性筛选 3-5 条深挖新闻
→ 分别抓取每条新闻原文并抽取素材卡
→ 分别判断每条新闻的文章类型：主线型 / 解读型 / 工具型
→ 为每条新闻分别生成更有公众号感的标题、大纲和 Markdown 正文
```

## 编辑层验证规则
基础日报继续使用 `verify.py` 验证。深度阅读中间文件和最终 Markdown 成稿使用独立验证脚本。

`verify_deepread.py` 用于验证 `reports/YYYY-MM-DD.deepread.json`：

- 校验 JSON 是否合法。
- 校验顶层字段 `date`、`source_report`、`selected_items`。
- 校验深挖新闻数量是否为 3-5 条。
- 校验每条深挖新闻是否包含标题、URL、来源、文章类型、输出文件、抓取状态、选择原因和成稿计划。
- 校验文章类型是否只使用 `主线型`、`解读型`、`工具型`。
- 校验每条深挖新闻是否指向一篇真实存在的 `daily_paper/*.md`，且不同新闻不能共用同一篇 Markdown。

运行方式：

```bash
python verify_deepread.py reports/YYYY-MM-DD.deepread.json
```

`verify_article.py` 用于验证 `daily_paper/` 里的 Markdown 成稿，包含硬规则、类别结构和启发式编辑评分。

硬规则包括：

- 文件名必须符合 `YYYY-MM-DD-文章标题.md`。
- Markdown 必须以一级标题开头，且只能有一个一级标题。
- 至少包含 3 个二级标题。
- 正文篇幅不能过短。
- 最终 Markdown 正文不能出现参考来源区块、URL、媒体名、公众号名或平台来源名。

类别结构规则包括：

- 主线型应覆盖“发生了什么、为什么重要、影响或改变、后续观察”。
- 解读型应覆盖“趋势问题、背后原因、技术含义、影响判断”。
- 工具型应覆盖“工具定义、适合人群、技术看点、局限提醒”。

编辑评分用于粗略拦截“像说明文、没有观点、没有钩子、没有节奏”的稿子。评分维度包括开头钩子、观点感、故事线、技术深度和公众号可读性。硬规则失败直接失败；编辑评分低于 75 分视为需要重写，75-84 分可用但建议润色，85 分以上更接近可发布。

运行方式：

```bash
python verify_article.py daily_paper --deepread reports/YYYY-MM-DD.deepread.json --verbose
python verify_article.py daily_paper/YYYY-MM-DD-文章标题.md --deepread reports/YYYY-MM-DD.deepread.json
```

## 已知陷阱
- 不要收录纯营销或广告内容。
- 相关度评分低于 3 的内容不要收录。
- 同一篇文章不要重复收录，使用 URL 去重。
- 输出必须是合法 JSON。
- 每条必须有真实 URL，不要编造链接。
- 搜狗微信搜索结果必须解析发布时间；无发布时间或不是当天的公众号文章不要收录。
