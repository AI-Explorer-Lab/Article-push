# 微信公众号推送-harness（精简版 v2.0）

微信公众号 AI 技术内容生产 Harness 流水线 —— 从信息抓取到成稿校验的全流程自动化工具。

## 精简版变更

相比 v1.0：
- **移除 Anthropic News 抓取**（经常返回垃圾数据）
- **修复微信公众号搜狗搜索**（更稳定的解析逻辑）
- **简化流程**：不再先选 10 篇再挑 5 篇，agent.py 直接抓取并筛选 5 篇
- **增加 AI 质量评估**：获取原文后用 AI 判断好不好，好的生成 MD，不好跳过
- **逐篇处理**：每读完一篇生成完，立即丢弃上下文记忆
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
│   ├── writer.py                   # AI 质量评估 + 文章写作
│   └── pipeline.py                 # 流程编排
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
LLM_API_KEY=your_api_key
LLM_API_BASE=https://api.openai.com/v1
LLM_MODEL=gpt-4o
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

## 流水线工作流（精简版）

```
agent.py → reports/YYYY-MM-DD.json          （直接抓取 5 篇候选）
     ↓
deepread → deepread.json                     （确认选题计划）
     ↓
writer → AI 评估质量 + 逐篇生成 MD           （好的写，不好跳过）
     ↓
verify*.py → 质量校验                        （自动验证）
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
