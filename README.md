# 微信公众号推送-harness（v2.1 — 审稿修订版）

微信公众号 AI 技术内容生产 Harness 流水线 —— 从信息抓取到成稿校验的全流程自动化工具。

## 核心创新：双 Agent 架构

- **写作 Agent**（`LLM_API_KEY`）：负责阅读原文、生成初稿、根据审稿意见修改
- **审稿 Agent**（`REVIEW_LLM_API_KEY`）：独立的 LLM 实例，负责五维度审稿评分
- **为什么要两个 Agent？** 自己写自己打分容易 bias 高分；独立审稿 Agent 给出客观评价
- **审稿不通过怎么办？** 审稿建议喂回写作 Agent，修改后重新审稿，最多 N 轮，直到通过或达到上限

## v2.1 变更

相比 v2.0：
- **独立审稿 Agent**：用另一个 LLM（`REVIEW_LLM_*` 环境变量）做五维度审稿，避免「自己写自己打分」
- **审稿修订循环**：审稿不通过时，保留上下文记忆，写作 Agent 根据审稿建议修改后重新提交审稿
- **记忆生命周期**：原文上下文不再一写即丢，而是保留到审稿通过后才丢弃
- 审稿维度：hook（开头吸引力）、point_of_view（观点判断力）、storyline（结构推进感）、technical_depth（技术深度）、wechat_readability（公众号可读性）

## 精简版变更（v2.0）

相比 v1.0：
- **移除 Anthropic News 抓取**（经常返回垃圾数据）
- **修复微信公众号搜狗搜索**（更稳定的解析逻辑）
- **简化流程**：不再先选 10 篇再挑 5 篇，agent.py 直接抓取并筛选 5 篇
- **增加 AI 质量评估**：获取原文后用 AI 判断好不好，好的生成 MD，不好跳过
- **GitHub 最多 2 篇**，微信公众号/媒体/博客至少 3 篇

## 项目结构

```
src/
├── common/                         # 公共组件
│   ├── utils.py                    # 抓取、清洗、JSON 读写等工具函数
│   └── verifier.py                 # 统一验证器数据类 (VerifierResult)
│
├── infrastructure/                 # 基础设施（外部系统交互）
│   ├── llm_client.py               # LLM API 调用封装
│   └── error_logger.py             # 错误日志与建议规则
│
├── core/                           # 核心业务逻辑
│   ├── agent.py                    # 直接抓取 5 篇候选（GitHub≤2，其他≥3）
│   ├── context.py                  # 原文抓取与事实提取
│   ├── deepread.py                 # 选题计划生成
│   ├── writer.py                   # AI 质量评估 + 文章写作 + 审稿修订
│   └── pipeline.py                 # 流程编排（含审稿修订循环）
│
└── validators/                     # 验证层
    ├── verify.py                   # 日报 JSON 校验
    ├── verify_deepread.py          # Deepread 选题校验
    ├── verify_article.py           # Markdown 成稿质量校验
    └── verify_consistency.py       # 配置一致性检查

AGENT.md                 # 规则契约
harness.toml             # 结构化配置
reports/                 # 日报 JSON 产出
daily_paper/             # Markdown 成稿产出
errors/                  # 错误日志
states/                  # 运行状态
```

## 快速开始

### 环境要求

- Python >= 3.11

### 配置环境变量

创建 `.env` 文件（使用 LLM 评估和改写功能时需要）：

```bash
# 写作 Agent（生成文章和根据审稿意见修改）
LLM_API_KEY=your_api_key
LLM_API_BASE=https://api.openai.com/v1
LLM_MODEL=gpt-4o

# 审稿 Agent（可选，独立审稿评分，避免自己写自己打分）
# 如果不配置，会 fallback 到写作 LLM（至少是不同的调用实例）
REVIEW_LLM_API_KEY=your_review_api_key     # 可选：独立审稿 API Key
REVIEW_LLM_MODEL=gpt-4o                    # 可选：独立审稿模型
REVIEW_LLM_API_BASE=https://api.openai.com/v1  # 可选：独立审稿 API Base
```

### 一键运行全流水线

```bash
cd f:\微信公众号推送-harness
python -m src.core.pipeline --date 2026-05-28 --use-llm-writer
```

### 常用参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--date 2026-05-28` | 指定运行日期 | 今天 |
| `--days 10` | 抓取回溯天数 | 10 |
| `--skip-fetch` | 跳过抓取，直接用已有 report JSON | 否 |
| `--refresh-deepread` | 重新生成 deepread 选题 | 否 |
| `--overwrite-articles` | 覆盖已有 Markdown 文章 | 否 |
| `--use-llm-writer` | 启用 LLM 评估质量 + 改写文章 | 否 |
| `--llm-model gpt-4o` | 指定 LLM 模型 | 环境变量 LLM_MODEL |

示例：

```bash
# 跳过抓取，仅重新生成文章
python -m src.core.pipeline --skip-fetch --overwrite-articles

# 启用 LLM 评估和改写
python -m src.core.pipeline --days 10 --use-llm-writer
```

## 单独运行各阶段

### 抓取日报

```bash
python -m src.core.agent --date 2026-05-28 --days 10
```

产出：`reports/2026-05-28.json`（5 条，GitHub ≤ 2）

### 验证产出质量

```bash
# 验证日报 JSON
python src/validators/verify.py

# 验证 deepread 选题
python src/validators/verify_deepread.py

# 验证单篇 Markdown 文章
python src/validators/verify_article.py daily_paper/2026-05-28-01.md
```

## 流水线工作流

```
agent.py → reports/YYYY-MM-DD.json          （直接抓取 5 篇候选）
     ↓
deepread → deepread.json                     （确认选题计划）
     ↓
writer → AI 评估质量 + 逐篇生成 MD 初稿      （好的写，不好跳过）
     ↓
审稿 Agent → 五维度审稿评分                  （独立 LLM，避免 bias）
     ↓
  ├─ PASS → 丢弃上下文，进入验证
  └─ FAIL → 写作 Agent 根据审稿建议修改 → 再审（最多 N 轮）
     ↓
verify*.py → 质量校验                        （硬性规则最终门禁）
     ↓
errors/YYYY-MM-DD-log.md                    （失败归因）
```

## 配置说明

核心配置在 `harness.toml`：

- `[pipeline]` — 流水线参数（目标 5 篇，GitHub 最多 2）
- `[focus_topics]` — 关注领域及关键词别名
- `[llm]` — LLM 配置（含 AI 质量评估参数）
- `[article]` — 文章生成约束（字数、段落、句长等）
- `[fetch]` — 抓取配置（延迟、超时等）

## 审稿修订流程详解

1. **生成初稿**：写作 Agent 读完原文后生成 Markdown 初稿
2. **独立审稿**：审稿 Agent 从 5 个维度打分（hook/point_of_view/storyline/technical_depth/wechat_readability），总分 100，75 分通过
3. **不通过时**：上下文记忆保留，审稿建议（2-5 条具体修改意见）喂回写作 Agent
4. **修改再审**：写作 Agent 根据建议修改文章，重新提交审稿
5. **循环上限**：默认最多 3 轮修订（可通过 `--llm-rewrite-attempts` 调整）
6. **记忆清理**：全部文章审稿通过后，统一丢弃所有原文上下文
