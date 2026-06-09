# AGENT.md - 微信公众号推送 Harness 规则契约（v3.0）

## 任务目标
追踪前沿 AI 技术动态，生成可验证、可归档、可发布到公众号的文章。

## v2.4 质量增强要点
- **AI 摘要**：summary 由 LLM 生成 80-120 字中文摘要（不再截断原文前100字符）
- **AI 洞察**：insight 由 LLM 基于原文内容生成独立判断（不再使用硬编码模板）
- **智能分类**：classify 优先使用 LLM 语义分类（不再仅用关键词匹配）
- **精简 prompt**：writer prompt 减少模板腔引导，让 LLM 从原文中提取具体细节
- **轻量润色**：polish 仅做段落拆分，模板腔规避已交给写作 prompt 约束和审稿 Agent 判断

## 核心原则（精简后）
- **直接选 5 篇**：agent.py 直接抓取并筛选 5 篇候选
- **GitHub 最多 2 篇**，微信公众号/媒体/博客至少 3 篇
- **AI 评估质量**：获取链接元数据后，用 AI 阅读原文判断好不好，好的生成 MD，不好就跳过
- **逐篇处理**：每读完一篇生成完，保留上下文记忆直到审稿 Agent 通过后才丢弃
- **来源真实**：所有 URL 必须真实可访问
- **全链路内聚**：agent.py 内聚抓取→阅读→写作→审稿→保存全部逻辑，无中间层
- **审稿修订有明确边界**：每篇最多 N 轮修改，连续退化自动终止

## 信息源
优先搜索以下来源：
- 微信公众号（通过本机微信前台短接管采集 URL；macOS 走 OCR/AppleScript，Windows 走截图优先视觉接管或缓存 fallback）：配置在 `src/constants/wechat_sources.py` 中
- GitHub Trending/Search：AI 相关热门仓库（最多 2 篇）
- 网页信息源：配置在 `src/constants/info_sources.py` 中
- WordPress API 源：配置在 `src/constants/info_sources.py` 中

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
      "source": "微信公众号 | GitHub | 网页信息源（见 info_sources.py 配置）",
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

## 审稿修订循环规则（v2.3 新增）
每篇文章的审稿→修改→再审流程：

**轮数上限**：
- 默认最多 3 轮（通过 `harness.toml` 的 `[llm].review_max_rounds` 配置）
- 可通过 `--llm-rewrite-attempts N` 命令行参数临时覆盖

**逐轮策略升级**：
- 第 1 轮：gentle（温和修改，temperature 0.5）— 针对性修补扣分项
- 第 2 轮：moderate（强化修改，temperature 0.7）— 更大幅度改写段落和标题
- 第 3+ 轮：aggressive（强力重构，temperature 0.8）— 允许重写开头、重组结构

**退化检测**：
- 连续 3 轮评分不升反降 → 提前终止修改，标记为 `review_degraded`
- 上轮评分下降超过 5 分 → 自动跳过当前策略档位，进入下一档强度

**通过门槛**：
- 审稿评分 ≥ 65 分即通过（通过 `harness.toml` 的 `[llm].review_pass_score` 配置）
- 审稿 Agent 使用独立的 `REVIEW_LLM_*` 环境变量，与写作 LLM 隔离

## 公众号推文编辑规则
每条深挖新闻生成一篇独立 Markdown，保存为 `daily_paper/YYYY-MM-DD-文章标题.md`。

生成文章时逐篇处理：
- 先用 AI 阅读原文评估质量（好不好）
- 好的就生成 MD 文章，不好就跳过下一个
- 初稿生成后保留原文上下文，等待审稿 Agent 独立评审
- 审稿通过后才丢弃上下文；不通过则带着审稿建议修改后再审（最多 N 轮）
- 不同文章之间不能复用上一篇的事实、表达套路或判断框架

LLM 写作层规则：
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
python -m src.core.pipeline --date YYYY-MM-DD
```

流程：
```
读取 AGENT.md
-> agent.py 直接抓取 5 篇候选（GitHub 最多 2，其他至少 3）
-> 逐篇：抓原文 → AI 阅读 → 质量评估 → 写作 → 审稿修订循环:
    审稿 Agent 独立评审 → 不通过则修改 → 再审
    - gentle (R1) → moderate (R2) → aggressive (R3+)
    - 连续 3 轮评分下降 → 提前终止
-> 审稿通过后丢弃上下文记忆
-> verify.py 验证基础日报
-> verify_article.py 逐篇验证
```

## 短期状态规则
当天 pipeline 运行状态写入 `states/YYYY-MM-DD.article_states.json`。
状态文件不得保存原文全文。

## 运行问题沉淀规则

失败时写入 `errors/YYYY-MM-DD-log.md`，不自动修改 AGENT.md。

**日志分析 Agent**：
- 写入错误日志时，`error_logger.py` 先用正则匹配已知坑位（快速路径）
- 若未命中已知坑位，自动调用 LLM 日志分析 Agent 进行智能分析
- LLM 分析 Agent 使用独立的 `LOG_ANALYZER_LLM_*` 环境变量（fallback 到 `LLM_*`）
- AI 分析结果标记 `[AI 分析]` / `[AI 建议]` 前缀，需人工核查后再写入 AGENT.md

## 已知陷阱
- 不要收录纯营销或广告内容
- 不要编造 URL
- 微信公众号文章必须从真实 `mp.weixin.qq.com` URL 抓取，并记录可验证发布时间
- Anthropic News 不再抓取
- 输出必须是合法 JSON
