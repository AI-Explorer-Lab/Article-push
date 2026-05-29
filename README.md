# 微信公众号推送 Harness（v3.0）

> **一个标准的 Harness Engineering 实现** —— 从信息抓取到成稿校验的全流程自动化内容生产系统。

---

## 🏗️ Harness 架构对齐

本项目严格遵循 **Harness Engineering** 的六大核心组件范式。每一层都有明确的代码归属和职责边界：

```
┌─────────────────────────────────────────────────────────────────────┐
│  ① 上下文精细化 (Context Refinement)                                 │
│     AGENT.md — 规则契约：写作风格、选题领域、审稿门槛、输出格式       │
│     harness.toml — 结构化配置：字数约束、温度参数、轮数上限、超时      │
│     src/core/context.py — 原文抓取与事实提取，为 LLM 注入精确上下文   │
│     src/core/agent.py — 逐篇处理时保留原文上下文，审稿通过后才丢弃     │
├─────────────────────────────────────────────────────────────────────┤
│  ② 工具系统 (Tool System)                                           │
│     src/infrastructure/browser_fetcher.py — Selenium 搜狗微信搜索    │
│     src/infrastructure/llm_client.py — LLM API 封装（写作/审稿/分析） │
│     src/common/utils.py — URL 清洗、JSON 读写、文本处理等工具函数     │
├─────────────────────────────────────────────────────────────────────┤
│  ③ 执行编排 (Execution Orchestration)                                │
│     src/core/pipeline.py — 编排层：Stage 串行管理 + 验证门禁         │
│     src/core/agent.py — Worker 层：抓取→阅读→写作→审稿→保存全链路    │
│     └─ Stage 1: agent 全链路生成                                     │
│     └─ Stage 2: verify.py 日报 JSON 校验                             │
│     └─ Stage 3: verify_article.py 成稿质量校验                       │
├─────────────────────────────────────────────────────────────────────┤
│  ④ 记忆与状态 (Memory & State)                                       │
│     states/YYYY-MM-DD.article_states.json — 当天运行状态（不含原文）  │
│     reports/YYYY-MM-DD.json — 日报结构化数据（标题/URL/分类/摘要）    │
│     agent.py 上下文记忆 — 每篇文章的原文全文在审稿期间驻留内存        │
│       └─ 审稿通过 → 丢弃  |  审稿不通过 → 带建议修改 → 再审          │
├─────────────────────────────────────────────────────────────────────┤
│  ⑤ 评估与观测 (Evaluation & Observability)                           │
│     src/middleware/pipeline_logger.py — 进度日志：时间戳+阶段计时     │
│     logs/pipeline-YYYY-MM-DD.log — 持久化日志文件                    │
│     src/validators/verify_article.py — 审稿 Agent 5维评分            │
│       └─ hook/point_of_view/storyline/technical_depth/wechat_readability│
│     src/infrastructure/error_logger.py — 错误归因：已知坑位匹配 + LLM 智能分析│
├─────────────────────────────────────────────────────────────────────┤
│  ⑥ 约束与恢复 (Constraint & Recovery)                                │
│     src/validators/ — 质量门禁：硬性规则校验，不通过即阻断           │
│       ├─ verify.py           — 日报 JSON 必填字段/格式/URL去重校验   │
│       ├─ verify_article.py   — Markdown 硬性规则：文件名/字数/禁止词  │
│       └─ verify_consistency.py — 配置一致性检查                     │
│     agent.py 退化检测 — 连续3轮评分下降 → 提前终止                   │
│     --overwrite / --skip-fetch — 手动恢复入口                        │
└─────────────────────────────────────────────────────────────────────┘
```

### 组件 ↔ 代码映射表

| # | Harness 组件 | 核心文件 | 职责 |
|---|-------------|---------|------|
| ① | **上下文精细化** | `AGENT.md` + `harness.toml` + `context.py` + `agent.py` | 规则契约定义、结构化配置、原文事实提取、上下文窗口管理 |
| ② | **工具系统** | `browser_fetcher.py` + `llm_client.py` + `utils.py` | 浏览器搜索抓取、LLM API 调用、通用工具函数 |
| ③ | **执行编排** | `pipeline.py` + `agent.py` | 三阶段流水线编排、全链路 Worker 执行 |
| ④ | **记忆与状态** | `states/` + `reports/` + agent 内存 | 运行状态持久化、日报数据存储、审稿期间上下文记忆 |
| ⑤ | **评估与观测** | `pipeline_logger.py` + `verify_article.py`(审稿Agent) + `error_logger.py` | 阶段计时日志、5维审稿评分、错误智能归因 |
| ⑥ | **约束与恢复** | `verify.py` + `verify_article.py`(硬性规则) + `verify_consistency.py` + 退化检测 | 质量门禁阻断（ERROR级别）、评分退化提前终止、手动恢复机制 |

---

## 项目结构

```
src/
├── common/                         # 公共组件
│   ├── utils.py                    # 抓取、清洗、JSON 读写等工具函数
│   └── verifier.py                 # 统一验证器数据类 (VerifierResult)
│
├── constants/                      # 用户常量配置（按需修改，保护隐私）
│   ├── wechat_sources.py           # 微信公众号搜索源（不暴露个人信息）
│   ├── info_sources.py             # 网页源/模板/洞察等通用配置（不暴露数据源）
│   ├── wechat_sources.py.bk        # 个人备份（.gitignore 已排除）
│   └── info_sources.py.bk          # 个人备份（.gitignore 已排除）
│
├── infrastructure/                 # 基础设施 — ② 工具系统
│   ├── llm_client.py               # LLM API 调用封装（写作/审稿/日志分析）
│   ├── browser_fetcher.py          # Selenium 浏览器抓取（搜狗微信搜索）
│   └── error_logger.py             # 错误日志 + 已知坑位匹配 + LLM 智能归因
│
├── core/                           # 核心业务逻辑
│   ├── agent.py                    # 全链路 Worker：③执行编排 + ④记忆管理
│   ├── pipeline.py                 # 编排层：Stage 管理 + 验证门禁
│   └── context.py                  # 原文抓取与事实提取 — ①上下文精细化
│
├── middleware/                      # 中间件 — ⑤ 评估与观测
│   └── pipeline_logger.py          # Pipeline 进度日志：时间戳 + 阶段计时
│
└── validators/                     # 验证层 — ⑤评估 + ⑥约束
    ├── verify.py                   # 日报 JSON 校验（⑥ 硬性门禁）
    ├── verify_article.py           # 成稿质量：硬性规则(⑥) + 审稿Agent评分(⑤)
    └── verify_consistency.py       # 配置一致性检查（⑥）

AGENT.md                 # ① 规则契约（Policy as Code）
harness.toml             # ① 结构化配置（Config as Code）
reports/                 # ④ 日报 JSON 产出
daily_paper/             # Markdown 成稿产出
errors/                  # ⑤ 错误日志
states/                  # ④ 运行状态
logs/                    # ⑤ Pipeline 运行日志
```

---

## 快速开始

### 环境要求

- Python >= 3.11

### 配置环境变量

创建 `.env` 文件：

```bash
# 写作 Agent
LLM_API_KEY=your_api_key
LLM_API_BASE=https://api.openai.com/v1
LLM_MODEL=gpt-4o

# 审稿 Agent（可选，独立审稿评分）
REVIEW_LLM_API_KEY=your_review_api_key
REVIEW_LLM_API_BASE=https://api.openai.com/v1
REVIEW_LLM_MODEL=gpt-4o

# 错误日志分析 Agent（可选，如果未配置则用 LLM_API_* 配置）
REVIEW_LLM_API_KEY=your_review_api_key
REVIEW_LLM_API_BASE=https://api.openai.com/v1
REVIEW_LLM_MODEL=gpt-4o
```
### 一键运行全流水线

```bash
python -m src.core.pipeline
```

默认抓取**当天**文章、生成 5 篇、逐篇审稿验证。

### 常用参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--date 2026-05-29` | 指定运行日期 | 今天 |
| `--days 1` | 抓取回溯天数 | 1（仅当天） |
| `--skip-fetch` | 跳过抓取，直接用已有 report JSON | 否 |
| `--overwrite` | 强制重新生成，覆盖已有文章 | 否 |

示例：

```bash
# 覆盖已有文章重新生成
python -m src.core.pipeline --overwrite

# 抓取近 3 天的文章
python -m src.core.pipeline --days 3

# 跳过抓取，仅验证已有产出
python -m src.core.pipeline --skip-fetch
```

---

## 流水线工作流

```
pipeline.py (③ 执行编排层)
  │
  ├─ Stage 1: agent.py (全链路 Worker)
  │     ├─ ② 浏览器搜狗搜索 / GitHub 抓取候选
  │     ├─ ① 逐篇：抓原文 → AI 阅读(上下文精细化) → 质量评估 → 写作
  │     ├─ ⑤ 审稿 Agent 独立评审（5维评分，≥65分通过）
  │     │   └─ 不通过 → 带建议修改 → 再审（最多3轮，逐轮策略升级）
  │     │   └─ ⑥ 连续退化 → 提前终止
  │     ├─ ④ 审稿通过 → 丢弃上下文记忆 → 保存成稿
  │     └─ 产出: reports/{date}.json + daily_paper/{date}-*.md
  │
  ├─ Stage 2: verify.py (⑥ 日报 JSON 门禁 — 硬性规则)
  │     └─ 必填字段、URL 格式、分类完整性
  │
  └─ Stage 3: verify_article.py (⑤+⑥ 成稿质量校验)
        └─ ⑥ 硬性规则：Markdown 格式、字数、禁止词
        └─ ⑤ 审稿 Agent：5维评分（hook/观点/线索/深度/可读性）+ 模板腔检测
              │
              ├─ PASS → 流水线完成
              └─ FAIL → errors/{date}-log.md (⑤ 错误归因)
```

---

## 日志追踪（⑤ 评估与观测）

每次运行同时输出到控制台和 `logs/pipeline-YYYY-MM-DD.log`：

```
[10:54:07] [INFO] ============================================================
[10:54:07] [INFO]   Pipeline Start: 2026-05-29
[10:54:07] [INFO] ============================================================
[10:54:07] [INFO] 加载规则契约 AGENT.md ...
[10:54:07] [INFO] AGENT.md 已加载 (3866 chars)
[10:54:07] [INFO] Stage 1/3: 启动 agent 全链路生成...
[10:54:07] [INFO] ▶ 开始阶段: run agent (full pipeline)
...
[11:20:15] [INFO] ✔ 完成阶段: run agent (full pipeline) (耗时 1568.3s)
```

---

## 单独运行各模块

### 只跑 agent（生成报告+文章）

```bash
python -m src.core.agent --date 2026-05-29
```

### 验证产出质量

```bash
# 验证日报 JSON
python -m src.validators.verify reports/2026-05-29.json

# 验证单篇 Markdown 文章
python -m src.validators.verify_article daily_paper/2026-05-29-01.md --verbose
```

---

## 配置说明

### 微信公众号搜索源配置

编辑 `src/constants/wechat_sources.py`，填入你要追踪的公众号：

```python
WECHAT_SOURCES: list[tuple[str, str]] = [
    ("你的公众号名称", "搜索关键词"),
]

WECHAT_ACCOUNT_IDS: dict[str, str] = {
    "你的公众号名称": "",  # Account ID 可选，不知道就留空
}
```

### 网页信息源与模板配置

编辑 `src/constants/info_sources.py`，配置网页抓取源、模板模式等：

```python
# 网页信息源（名称, URL, 匹配pattern）
WEB_SOURCES: list[tuple[str, str, str]] = [
    ("示例技术博客", "https://example.com/articles", "example"),
]

# WordPress API 源（有公开 JSON API 的站点）
WP_API_SOURCES: list[dict] = [
    # {"name": "...", "url": "...", "display_name": "...", "limit": 6},
]
```

**隐私保护**：禁止词和正文噪声清洗规则会自动从你的配置生成，无需手动维护。
如需备份自己的真实配置，复制到 `*.py.bk`（已被 `.gitignore` 排除，不会提交到 Git）。

### 禁止词规则说明

禁止词检查区分两种场景：
- **搬运腔**：拦截 `据{来源名}报道`、`{来源名}称`、`援引{来源名}` 等搬运原文的表述
- **正常提及**：放行 `{来源名}举办了xxx`、`{来源名}发布了xxx` 等正常的新闻叙述

### 流水线与文章配置

核心配置在 `harness.toml`（① 上下文精细化 - Config as Code）：

| 区块 | 说明 |
|------|------|
| `[pipeline]` | 流水线参数（目标 5 篇，GitHub 最多 2） |
| `[focus_topics]` | 关注领域及关键词别名 |
| `[llm]` | LLM 配置、审稿轮数/门槛、日志分析开关 |
| `[article]` | 文章生成约束（字数、段落、句长等） |
| `[fetch]` | 抓取配置（延迟、超时等） |

规则约束在 `AGENT.md`（① 上下文精细化 - Policy as Code）：写作风格、选题范围、审稿规则、输出格式。

---

## 审稿修订循环（⑤ 评估 + ⑥ 约束）

1. **生成初稿**：写作 Agent 读完后生成 Markdown 初稿
2. **独立审稿**：审稿 Agent 5 维打分（hook/point_of_view/storyline/technical_depth/wechat_readability），总分 100，**≥65 分通过**
3. **不通过时**：④ 上下文记忆保留，审稿建议喂回写作 Agent
4. **修改再审**：根据建议修改，重新提交审稿
5. **逐轮策略升级**：gentle (R1, t=0.5) → moderate (R2, t=0.7) → aggressive (R3+, t=0.8)
6. **退化检测**：连续 3 轮评分下降 → ⑥ 提前终止，标记 `review_degraded`
7. **循环上限**：默认最多 3 轮修订
8. **记忆清理**：全部文章审稿通过后，统一丢弃所有原文上下文

---

## 架构演进

| 版本 | 架构 | 关键变化 |
|------|------|----------|
| v1.0 | 三段式 pipeline | agent → deepread → writer，阶段间靠 JSON 通信 |
| v2.0 | 逐条全链路 | agent 内聚抓取→阅读→写作→审稿，不再需要中间 JSON |
| v3.0 | 编排层 | pipeline 瘦身为纯编排+验证层，完整对齐 Harness 六大组件 |

## v3.0 变更

相比 v2.x：
- **pipeline.py 瘦身**：从 536 行精简到 ~370 行，只做编排+验证+日志
- **移除 writer.generate_articles() 调用**：agent.py 自己写文章了
- **移除审稿修订循环**：agent.py 内置了逐篇审稿→修订→再审的完整流程
- **新增 middleware/pipeline_logger.py**：带时间戳的阶段计时日志，同时写控制台+文件
- **搜狗微信搜索优化**：自动点击"按时间排序"，默认只看当天文章（`--days 1`）
- **修复 kept_existing 分支**：已有文章时从 article_states 回读真实元数据，避免验证失败
- **模板化判断 Agent 化**：模板腔规避约束写入写作 prompt，模板腔检测交给审稿 Agent 判断，不再靠硬编码正则替换/匹配
