"""src/core/deepread.py - Deepread 选题生成模块（精简版）。

新架构（精简后）：
- agent.py 已经直接选了 5 篇候选（GitHub 最多 2，其他至少 3）
- deepread 只负责：确定文章类型、标题、输出文件
- 不再先选 10 篇再挑 5 篇
"""

from __future__ import annotations

import re
from pathlib import Path

from src.common.utils import load_json, slugify, write_json
from src.core.context import extract_required_terms

# 文章类型风格契约
STYLE_CONTRACTS = {
    "主线型": {
        "position": "围绕一条具体事件推进，先讲清楚原文发生了什么，再解释为什么重要。",
        "must_answer": ["发生了什么", "谁做了什么", "为什么值得单独写", "后续应该看什么"],
        "tone": "事实密度优先，判断跟在事实后面，不写空泛趋势口号。",
    },
    "解读型": {
        "position": "围绕一个趋势或技术信号展开，原文事实是证据，文章重点是解释变化背后的原因。",
        "must_answer": ["趋势是什么", "原文事实如何支撑趋势", "技术含义是什么", "对行业或开发者有什么影响"],
        "tone": "解释要克制，避免把单条新闻拔高成确定趋势。",
    },
    "工具型": {
        "position": "围绕工具、项目或产品展开，先说明它是什么，再说明适合谁、边界在哪里。",
        "must_answer": ["它是什么", "适合谁", "技术看点是什么", "局限和验证点是什么"],
        "tone": "少写宣传词，多写使用场景、失败路径和工程约束。",
    },
}


def infer_article_type(item: dict) -> str:
    """根据条目信息推断文章类型。"""
    source = str(item.get("source", ""))
    title = str(item.get("title", ""))
    category = str(item.get("category", ""))
    url = str(item.get("url", ""))
    text = f"{source} {title} {category} {url}".lower()
    if "github.com" in text or "github" in source.lower():
        return "工具型"
    if any(word in text for word in ["sdk", "api", "mcp", "context", "harness", "openai"]):
        return "解读型"
    return "主线型"


def article_title(item: dict, article_type: str) -> str:
    """根据条目和类型生成文章标题。

    只做基础清理，不再拼接固定后缀模板。
    最终标题由 LLM 在写作时根据原文内容自由生成。
    """
    title = str(item.get("title", "")).strip(" .")
    # 清理 GitHub 前缀和多余标点
    compact = re.sub(r"GitHub 项目更新：", "", title)
    compact = compact.replace("！", "").replace("？", "")
    return compact[:50]


def source_subject(item: dict) -> str:
    """从条目中提取主题短名。"""
    title = str(item.get("title", "这条 AI 动态"))
    title = re.sub(r"GitHub 项目更新：", "", title)
    return title.strip(" .")[:42] or "这条 AI 动态"


def unique_output_file(day: str, title: str, item: dict, used_outputs: set[str]) -> str:
    """为文章生成唯一的输出文件路径。"""
    base = slugify(title)
    output = f"daily_paper/{day}-{base}.md"
    if output not in used_outputs:
        used_outputs.add(output)
        return output

    hint = slugify(source_subject(item), max_len=18)
    output = f"daily_paper/{day}-{base}-{hint}.md"
    counter = 2
    while output in used_outputs:
        output = f"daily_paper/{day}-{base}-{hint}-{counter}.md"
        counter += 1
    used_outputs.add(output)
    return output


def selection_reason(item: dict, article_type: str) -> str:
    """生成选择理由。"""
    category = item.get("category", "AI")
    if article_type == "工具型":
        return f"该条目具备工具或项目属性，且与 {category} 相关，适合写成工程落地和边界分析。"
    if article_type == "解读型":
        return f"该条目体现开发者入口、上下文或基础设施变化，适合围绕 {category} 做趋势解读。"
    return f"该条目相关度较高，具备事件切入和传播冲突点，适合围绕 {category} 写成主线型文章。"


def core_claim(item: dict, article_type: str) -> str:
    """生成文章核心判断。"""
    category = item.get("category", "AI")
    if article_type == "工具型":
        return "Agent 工具的价值要落到状态、权限、部署、失败和审查这些生产问题上。"
    if article_type == "解读型":
        return "这类动态的重点不是单点功能，而是 AI 正在争夺进入真实系统的工程入口。"
    return f"这次事件暴露了 {category} 进入工程化阶段后必须处理的新门槛。"


def build_deepread(report_path: Path, deepread_path: Path, article_count: int = 5) -> None:
    """从基础日报生成 deepread 选题文件（精简版）。

    不再先选 10 篇再挑 5 篇。agent.py 已经直接选了 5 篇候选，
    这里只需要确认文章类型、生成标题和输出文件。
    """
    report = load_json(report_path)
    items = report.get("items", [])

    # 直接取前 article_count 篇（agent.py 已做好筛选）
    items = items[:article_count]

    selected = []
    used_outputs: set[str] = set()
    for item in items:
        article_type = infer_article_type(item)
        title = article_title(item, article_type)
        output_file = unique_output_file(report["date"], title, item, used_outputs)
        source_text = " ".join(
            str(item.get(key, "")) for key in ["title", "summary", "insight", "category"]
        )
        must_include = extract_required_terms(source_text, item.get("url", ""))
        selected.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "source": item.get("source", ""),
                "article_type": article_type,
                "output_file": output_file,
                "raw_text_status": "fetched",
                "selection_reason": selection_reason(item, article_type),
                "rewrite_policy": {
                    "based_on_original": True,
                    "memory_isolation": True,
                    "style_contract": STYLE_CONTRACTS[article_type]["position"],
                    "must_answer": STYLE_CONTRACTS[article_type]["must_answer"],
                },
                "article_plan": {
                    "title": title,
                    "core_claim": core_claim(item, article_type),
                    "must_include": must_include,
                    "original_summary": item.get("summary", ""),
                    "original_insight": item.get("insight", ""),
                },
            }
        )

    deepread = {
        "date": report["date"],
        "source_report": str(report_path.relative_to(report_path.parent.parent)).replace("/", "\\"),
        "generation_rule": (
            "由 deepread 模块从基础日报直接生成选题。agent.py 已直接选取 5 篇候选"
            "（GitHub 最多 2 篇，其他至少 3 篇），不再经过两轮筛选。"
            "每篇独立读取原文、独立判断质量、独立生成，上下文保留到审稿 Agent 通过后才丢弃。"
        ),
        "harness_episode": {
            "task_spec": "AGENT.md",
            "context_selection": str(report_path.relative_to(report_path.parent.parent)).replace("/", "\\"),
            "state_file": f"states/{str(report['date'])}.article_states.json",
            "verification": ["verify_deepread.py", "verify_article.py"],
            "completion_rule": "来源真实、选择有据、文章独立、验证通过、失败可归因。",
        },
        "selection_criteria": {
            "relevance": "优先选择相关度高、贴合 AI Agent、MCP、Coding Agent、Harness Engineering 的条目。",
            "writeability": "优先选择有明确问题、工具边界、工程启发或趋势判断的条目。",
            "composition": "GitHub 项目最多 2 篇，微信公众号/媒体/博客至少 3 篇。",
            "source_access": "优先使用已有 evidence 或可访问 URL 的条目。",
        },
        "selected_items": selected,
    }
    write_json(deepread_path, deepread)
    print(
        f"Generated {deepread_path.relative_to(deepread_path.parent.parent)} "
        f"with {len(selected)} selected items"
    )
