"""src/infrastructure/error_logger.py - 错误日志生成模块。

职责：
- 分析失败阶段并生成建议沉淀规则
- 规则匹配失败时，自动调用 LLM 日志分析 Agent 智能分析
- 写入 errors/YYYY-MM-DD-log.md
- 永不自动修改 AGENT.md
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.infrastructure.llm_client import LLMProvider


@dataclass
class StageResult:
    """统一的阶段结果数据结构。"""
    name: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


# ---------------------------------------------------------------------------
# 已知坑位规则匹配（快速路径，不需要 LLM）
# ---------------------------------------------------------------------------

_KNOWN_PATTERNS: list[tuple[str, str]] = [
    (
        r"UnicodeEncodeError|gbk.*codec",
        "运行 Python 验证脚本时设置 PYTHONIOENCODING=utf-8，避免 Windows 控制台编码导致误失败。",
    ),
    (
        r"Markdown 必须以一级标题开头",
        "写入 Markdown 时必须使用无 BOM UTF-8，并确保文件首字符就是 '# '。",
    ),
    (
        r"GitHub 项目类文章必须",
        "GitHub 项目类文章必须在开头 8 行内保留项目链接。",
    ),
    (
        r"output_file 重复",
        "自动生成 deepread 时必须对 output_file 去重；同类泛化标题需要追加新闻关键词或序号。",
    ),
    (
        r"超短段落比例偏高",
        "公众号成稿应合并连续超短段落，优先使用 2-4 句话组成一个自然段。",
    ),
    (
        r"编辑评分低于发布门槛",
        "成稿验证分数低于 65 时，不进入发布态，先按 verify_article.py 的 rewrite_suggestions 润色。",
    ),
]


def _match_known_patterns(failure_text: str) -> list[str]:
    """用正则匹配已知坑位，返回匹配到的规则列表。"""
    lessons: list[str] = []
    for pattern, lesson in _KNOWN_PATTERNS:
        if re.search(pattern, failure_text):
            lessons.append(lesson)
    return lessons


def _build_failure_text(results: list[StageResult]) -> str:
    """拼接所有失败阶段的输出为一段文本。"""
    parts: list[str] = []
    for result in results:
        if not result.ok:
            parts.append(f"--- 阶段：{result.name} (退出码 {result.returncode}) ---")
            output = (result.stdout + "\n" + result.stderr).strip()
            parts.append(output if output else "(no output)")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# LLM 日志分析 Agent
# ---------------------------------------------------------------------------

_ANALYSIS_SYSTEM_PROMPT = """你是一个 Harness 流水线的故障分析 Agent。你的任务是根据流水线失败日志，分析出可能的根因，并给出「建议沉淀的规则」。

背景：这是一个微信公众号 AI 技术内容生产流水线，流程为：
1. agent.py 抓取候选并逐篇生成文章（抓取→阅读→写作→审稿→保存） → 2. 验证基础日报 → 3. 逐篇验证 MD 成稿

你需要：
1. 从失败日志中提取关键错误信息
2. 判断是代码 bug、配置问题、数据源问题还是验证规则问题
3. 用简洁的中文给出 1-3 条「建议沉淀规则」（每条一句话）
4. 如果日志信息不足以判断，诚实地说"信息不足，需人工排查"

输出格式（严格 JSON）：
{
  "root_cause": "根因简述",
  "category": "代码bug | 配置问题 | 数据源问题 | 验证规则问题 | 未知",
  "lessons": ["规则1", "规则2"],
  "confidence": "high | medium | low"
}"""


def _create_log_analyzer() -> "LLMProvider | None":
    """创建日志分析 LLM provider。

    优先使用独立的 LOG_ANALYZER_LLM_* 环境变量，
    若未配置则尝试复用 LLM_* 变量，
    都不可用则返回 None（不启用 LLM 分析）。
    """
    try:
        from src.infrastructure.llm_client import OpenAICompatibleProvider

        api_key = os.environ.get("LOG_ANALYZER_LLM_API_KEY") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None

        return OpenAICompatibleProvider(
            api_base=os.environ.get("LOG_ANALYZER_LLM_API_BASE") or os.environ.get("LLM_API_BASE"),
            api_key=api_key,
            model=os.environ.get("LOG_ANALYZER_LLM_MODEL") or os.environ.get("LLM_MODEL"),
            max_retries=2,
            request_timeout=60,
        )
    except Exception:
        return None


def _analyze_with_llm(failure_text: str, analyzer: "LLMProvider") -> dict | None:
    """使用 LLM 分析失败日志，返回结构化分析结果。"""
    # 截断过长日志，避免超出 token 限制
    max_chars = 8000
    if len(failure_text) > max_chars:
        failure_text = failure_text[:max_chars] + "\n\n... (日志过长已截断)"

    try:
        raw = analyzer.chat(
            messages=[
                {"role": "system", "content": _ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": f"请分析以下流水线失败日志：\n\n```\n{failure_text}\n```"},
            ],
            temperature=0.2,
            max_tokens=800,
        )
        # 尝试提取 JSON
        json_match = re.search(r"\{[\s\S]*\}", raw)
        if not json_match:
            return {"root_cause": "LLM 返回格式异常", "category": "未知", "lessons": [], "confidence": "low", "raw": raw}
        return json.loads(json_match.group())
    except Exception as exc:
        return {"root_cause": f"LLM 分析调用失败: {exc}", "category": "未知", "lessons": [], "confidence": "low"}


def _format_llm_lessons(analysis: dict | None) -> list[str]:
    """将 LLM 分析结果格式化为可展示的规则列表。"""
    if not analysis:
        return []
    lessons: list[str] = []
    root_cause = analysis.get("root_cause", "")
    category = analysis.get("category", "未知")
    confidence = analysis.get("confidence", "low")
    llm_lessons = analysis.get("lessons", [])

    if root_cause:
        lessons.append(f"[AI 分析] 根因：{root_cause}（分类：{category}，置信度：{confidence}）")
    for lesson in llm_lessons:
        lessons.append(f"[AI 建议] {lesson}")
    if not root_cause and not llm_lessons:
        lessons.append("本次失败未匹配到已知坑，AI 分析亦未能给出有效建议，请人工阅读日志后决定是否补充新规则。")
    return lessons


# ---------------------------------------------------------------------------
# 统一入口：规则匹配 + LLM 分析（fallback）
# ---------------------------------------------------------------------------

def proposed_lessons(
    results: list[StageResult],
    *,
    enable_llm_analysis: bool = True,
) -> list[str]:
    """分析失败输出，生成建议沉淀的规则。

    策略：
    1. 先用正则匹配已知坑位（快速路径，零成本）
    2. 若未匹配到已知坑，且 enable_llm_analysis=True，则调用 LLM 日志分析 Agent
    3. LLM 分析失败或不可用时，给出兜底提示
    """
    failure_text = _build_failure_text(results)

    # 步骤 1：已知坑位匹配
    lessons = _match_known_patterns(failure_text)

    # 步骤 2：已知坑位未命中 → 尝试 LLM 分析
    if not lessons and enable_llm_analysis:
        analyzer = _create_log_analyzer()
        if analyzer:
            print("[日志分析 Agent] 已知坑位未匹配，启动 LLM 智能分析...")
            analysis = _analyze_with_llm(failure_text, analyzer)
            llm_lessons = _format_llm_lessons(analysis)
            if llm_lessons:
                lessons.extend(llm_lessons)
            else:
                lessons.append("本次失败未匹配到已知坑，AI 分析亦无结果，请人工阅读日志后决定是否补充新规则。")
        else:
            lessons.append("本次失败未匹配到已知坑，且未配置 LLM API（LOG_ANALYZER_LLM_* 或 LLM_*），请人工阅读日志后决定是否补充新规则。")

    if not lessons:
        lessons.append("本次失败未匹配到已知坑，请人工阅读日志后决定是否补充新规则。")

    return lessons


# ---------------------------------------------------------------------------
# 错误日志写入
# ---------------------------------------------------------------------------

def write_error_log(
    day: str,
    results: list[StageResult],
    errors_dir: Path,
    *,
    enable_llm_analysis: bool = True,
) -> Path:
    """写入错误日志到 errors/YYYY-MM-DD-log.md。"""
    errors_dir.mkdir(parents=True, exist_ok=True)
    path = errors_dir / f"{day}-log.md"
    failed = [result for result in results if not result.ok]
    lines = [
        f"# {day} pipeline 运行问题记录",
        "",
        f"- 记录时间：{datetime.now().isoformat(timespec='seconds')}",
        "- 状态：需要人工确认后再写入 AGENT.md",
        "- 用途：作为当次 harness episode 的失败归因材料，不自动改写长期规则",
        "",
        "## 失败阶段",
        "",
    ]
    for result in failed:
        lines.extend(
            [
                f"### {result.name}",
                "",
                f"- 命令：`{' '.join(result.command)}`",
                f"- 退出码：{result.returncode}",
                "",
                "```text",
                (result.stdout + "\n" + result.stderr).strip() or "(no output)",
                "```",
                "",
            ]
        )
    lines.extend(["## 建议沉淀规则", ""])
    for lesson in proposed_lessons(results, enable_llm_analysis=enable_llm_analysis):
        lines.append(f"- [ ] {lesson}")
    lines.extend(
        [
            "",
            "## 人工确认说明",
            "",
            "确认后再把勾选规则整理进 AGENT.md；pipeline.py 不会自动修改 AGENT.md。",
            "如规则由 AI 日志分析 Agent 生成，请重点核查其准确性后再采纳。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote failure log: {path}")
    return path
