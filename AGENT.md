# AGENT.md - 微信公众号推送 Harness 规则契约（精简版）

## 任务目标
追踪前沿 AI 技术动态，生成可验证、可归档、可发布到公众号的文章。

## 核心原则（精简后）
- **直接选 5 篇**：不再先抓 10 篇再挑 5 篇，agent.py 直接抓取并筛选 5 篇候选
- **GitHub 最多 2 篇**，微信公众号/媒体/博客至少 3 篇
- **AI 评估质量**：获取链接元数据后，用 AI 阅读原文判断好不好，好的生成 MD，不好就跳过
- **逐篇处理**：每读完一篇生成完，保留上下文记忆直到审稿 Agent 通过后才丢弃
- **来源真实**：所有 URL 必须真实可访问

## 信息源
优先搜索以下来源：
- 微信公众号（通过搜狗微信搜索）：Challenge Hub、量子位、AI学习的老章
- GitHub Trending/Search：AI 相关热门仓库（最多 2 篇）
- 机器之心
- 官方博客：OpenAI Blog、Google DeepMind Blog

不再抓取 Anthropic News。

## 关注领域
条目必须匹配至少一个领域：
- AI Agent（涵盖 Agentic AI、Multi-Agent 等）
- Harness Engineering
- Context Engineering
- MCP（Model Context Protocol）
- LLM推理与优化
- 多模态大模型
- AI Coding（涵盖 Codex、Claude Code、GitHub Copilot 等）
- Vibe Coding

## 输出格式要求
基础日报输出为 `reports/YYYY-MM-DD.json`，结构如下：

```json
{
  "date": "YYYY-MM-DD",
  "topic_focus": "...",
  "items": [
    {
      "title": "中文标题",
      "source": "微信公众号 | GitHub | 机器之心 | OpenAI Blog | Google DeepMind",
      "url": "https://...",
      "date": "YYYY-MM-DD",
      "category": "关注领域之一",
      "summary": "不超过 100 字",
      "insight": "不超过 150 字",
      "relevance": 1-5
    }
  ]
}
```

URL 必须真实；`relevance` 为 1-5，低于 3 的内容不要收录。

## 日报归档规则
- 默认归档到 `reports/YYYY-MM-DD.json`
- 同一 URL 不要重复收录
- 默认收录 5 条（GitHub 最多 2 条，微信公众号/媒体/博客至少 3 条）

## 深度阅读中间文件规则
深度阅读文件为 `reports/YYYY-MM-DD.deepread.json`，用于从基础日报中确认选题计划。

结构必须与 `verify_deepread.py` 和 `pipeline.py` 一致。`selected_items` 为 5 条。

## 公众号推文编辑规则
每条深挖新闻生成一篇独立 Markdown，保存为 `daily_paper/YYYY-MM-DD-文章标题.md`。

生成文章时逐篇处理：
- 先用 AI 阅读原文评估质量（好不好）
- 好的就生成 MD 文章，不好就跳过下一个
- 初稿生成后保留原文上下文，等待审稿 Agent 独立评审
- 审稿通过后才丢弃上下文；不通过则带着审稿建议修改后再审（最多 N 轮）
- 不同文章之间不能复用上一篇的事实、表达套路或判断框架

LLM 写作层规则：
- 启用 `pipeline.py --use-llm-writer` 时，先用 AI 评估质量，再生成文章
- LLM 必须基于当前条目的原文全文写作
- 原文全文只允许存在于单篇文章生成函数的局部上下文中
- 审稿 Agent（REVIEW_LLM_*）与写作 LLM 使用独立的 provider 实例，避免 bias
- 审稿全部通过后，统一丢弃所有原文全文

文章只使用三种形态：
- 主线型：围绕一条大新闻展开，事实密度优先
- 解读型：围绕趋势展开，原文事实是证据
- 工具型：围绕 GitHub 或产品工具展开

文章风格由类型契约控制，二级标题必须写成具体判断。

正文要求：
- 最终 Markdown 必须原创重写，不能搬运原文
- 不要出现"参考来源""来源""据某媒体报道"等来源归因区块
- 段落优先使用 2-4 句话组成自然段
- 二级标题不要直接写成功能标签

## Pipeline 入口
```bash
python -m src.core.pipeline --date YYYY-MM-DD [--use-llm-writer]
```

流程：
```
读取 AGENT.md
-> agent.py 直接抓取 5 篇候选（GitHub 最多 2，其他至少 3）
-> verify.py 验证基础日报
-> deepread 确认选题计划
-> writer 逐篇 AI 评估 + 生成 MD（好的写，不好跳过）
-> 审稿 Agent 独立评审 → 不通过则修改 → 再审（最多 N 轮）
-> 审稿通过后丢弃上下文记忆
-> verify_deepread.py + verify_article.py 验证
```

## 短期状态规则
当天 pipeline 运行状态写入 `states/YYYY-MM-DD.article_states.json`。
状态文件不得保存原文全文。

## 运行问题沉淀规则
失败时写入 `errors/YYYY-MM-DD-log.md`，不自动修改 AGENT.md。

## 已知陷阱
- 不要收录纯营销或广告内容
- 不要编造 URL
- 搜狗微信搜索结果必须解析发布时间
- Anthropic News 不再抓取
- 输出必须是合法 JSON
