"""src/validators/verify_article.py - daily_paper Markdown 成稿验证脚本。

审稿逻辑：
- 硬性规则（文件名、字数、禁止词、GitHub 链接等）：继续用规则检查
- 内容质量评估（结构、观点、可读性、模板腔等）：交给独立审稿 Agent 判断
- 审稿 Agent 使用独立的 LLM provider（REVIEW_LLM_* 环境变量），
  与写作 LLM 隔离，避免「自己写的自己打高分」的 bias
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.common.verifier import VerifierResult

load_dotenv()


VALID_ARTICLE_TYPES = {"主线型", "解读型", "工具型"}
PROHIBITED_PATTERNS = [
    ("参考来源", r"参考来源"),
    ("参考资料", r"参考资料"),
    ("参考链接", r"参考链接"),
    ("来源归因", r"来源[:：]"),
    ("据某方报道", r"据.{0,12}报道"),
    ("量子位", r"量子位"),
    ("机器之心", r"机器之心"),
    ("公众号名", r"Challenge Hub|AI学习的老章"),
]

OVERUSED_TEMPLATE_PATTERNS = [
    r"真正值得看的，不是标题里那一点热闹",
    r"这条 AI 动态，真正值得看的是工程入口",
    r"真正考验的是落地边界",
    r"AI 进入真实研发流程的问题摆到了台前",
    r"真正值得",
    r"真正考验",
    r"真正.*不是.*而是",
    r"这条动态值得拆开看",
    r"背后的技术信号",
    r"不只要看功能",
    r"不能只看功能",
    r"这件事到底改变了什么",
    r"先看它到底是什么",
    r"这不是孤立更新",
    r"最后落回一个简单问题",
    r"放回日常工作里",
]


def chinese_char_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def load_deepread(path: Path | None) -> dict[str, dict]:
    if not path:
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    mapping: dict[str, dict] = {}
    for item in data.get("selected_items", []):
        output_file = item.get("output_file")
        if output_file:
            mapping[Path(output_file).name] = item
    return mapping


def covered_terms(text: str, terms: list[str]) -> list[str]:
    covered: list[str] = []
    for term in terms:
        normalized = str(term).strip()
        if len(normalized) < 3:
            continue
        if normalized in text:
            covered.append(normalized)
    return covered


# ---------------------------------------------------------------------------
# 独立审稿 Agent
# ---------------------------------------------------------------------------

def _create_reviewer_provider():
    """创建审稿专用的 LLM provider。

    优先使用 REVIEW_LLM_* 环境变量（独立于写作 LLM），
    如果没有配置则 fallback 到写作 LLM（至少是不同的调用实例）。
    """
    from src.infrastructure.llm_client import OpenAICompatibleProvider

    review_key = (
        os.environ.get("REVIEW_LLM_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    review_model = (
        os.environ.get("REVIEW_LLM_MODEL")
        or os.environ.get("LLM_MODEL")
    )
    review_base = (
        os.environ.get("REVIEW_LLM_API_BASE")
        or os.environ.get("LLM_API_BASE")
        or "https://api.openai.com/v1"
    )
    if not review_key or not review_model:
        return None
    return OpenAICompatibleProvider(
        api_base=review_base,
        api_key=review_key,
        model=review_model,
    )


def review_with_agent(
    article_text: str,
    article_type: str,
    title: str,
    source_title: str,
    source_url: str,
    reviewer: object,
) -> dict:
    """用独立的审稿 Agent 对文章做综合质量评估。

    Args:
        article_text: 完整的 Markdown 文章
        article_type: 文章类型（主线型/解读型/工具型）
        title: 文章一级标题
        source_title: 原文标题
        source_url: 原文 URL
        reviewer: LLM provider 实例

    Returns:
        {
            "score": int (0-100),
            "checks": {"hook": {"score": int, "comment": str}, ...},
            "suggestions": [str, ...],
            "passed": bool,
            "raw_response": str,
        }
    """
    system = (
        "你是一名资深技术编辑和审稿人。你的任务是审读一篇公众号技术文章，"
        "从多个维度给出客观评价和具体修改建议。\n\n"
        "审稿原则：\n"
        "- 你不是在检查清单，而是在判断「这篇文章像不像一个真人编辑写出来的」\n"
        "- 重点看：有没有自己的判断视角、段落之间有没有自然的起承转合、"
        "二级标题是不是有具体信息而不是空泛概括、有没有模板腔和套话\n"
        "- 评分要诚实，不要因为文章看起来「格式正确」就给高分\n"
        "- 如果文章写得像 AI 生成的（四段论模板、空洞的连接词、缺少具体细节），分数要明显偏低\n"
    )

    user = f"""请审读下面这篇技术公众号文章，给出评分和修改建议。

文章类型：{article_type}
文章标题：{title}
原文标题：{source_title}
原文链接：{source_url}

请从以下五个维度分别打分（每项 0-10 分），并给出 1-2 句简短评语：

1. **hook（开头吸引力）**：开头 200 字是否快速抓住读者？是直接切入具体问题/矛盾/判断，还是泛泛的背景铺垫？
2. **point_of_view（观点判断力）**：文章是否有自己明确的判断，而不只是复述原文信息？判断是否克制、有分寸？
3. **storyline（结构推进感）**：二级标题是否包含具体信息？段落之间是否有自然的起承转合，而不是「首先其次最后」的模板堆砌？
4. **technical_depth（技术深度）**：是否有具体的工程概念、技术术语、使用场景或边界条件？有没有自然的类比或例子？
5. **wechat_readability（公众号可读性）**：段落长度是否适合手机阅读？语言是否自然、不像机器翻译？

然后给出：
- **overall_score**：总分 0-100（五项各 0-10，总分 = 五项之和 × 2）
- **verdict**：PASS（>=75 分）或 FAIL（<75 分）
- **suggestions**：2-5 条具体修改建议（中文，每条一句话，要具体可操作）

请严格按以下 JSON 格式输出，不要加其他内容：
```json
{{
  "hook": {{"score": 8, "comment": "开头直接切入矛盾，但第一句话可以更具体"}},
  "point_of_view": {{"score": 7, "comment": "有自己的判断但部分段落还是在复述"}},
  "storyline": {{"score": 6, "comment": "二级标题偏概括，缺少原文中的具体信息"}},
  "technical_depth": {{"score": 7, "comment": "有技术概念但缺少边界条件和失败场景的讨论"}},
  "wechat_readability": {{"score": 8, "comment": "段落长度合适，语言自然"}},
  "overall_score": 72,
  "verdict": "FAIL",
  "suggestions": [
    "第一条具体建议",
    "第二条具体建议"
  ]
}}
```

=== 待审文章 ===
{article_text[:8000]}
"""
    try:
        response = reviewer.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.3,
            max_tokens=1500,
        )
    except Exception as exc:
        return {
            "score": 0,
            "checks": {},
            "suggestions": [f"审稿 Agent 调用失败: {exc}"],
            "passed": False,
            "raw_response": str(exc),
        }

    # 解析 JSON 响应
    try:
        # 尝试提取 ```json ... ``` 代码块
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.S)
        if json_match:
            json_str = json_match.group(1).strip()
        else:
            json_str = response.strip()

        # 尝试找到最外层的 { ... } 对
        brace_start = json_str.find("{")
        brace_end = json_str.rfind("}")
        if brace_start != -1 and brace_end != -1:
            json_str = json_str[brace_start:brace_end + 1]

        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return {
            "score": 0,
            "checks": {},
            "suggestions": [f"审稿 Agent 返回格式异常，无法解析: {response[:300]}"],
            "passed": False,
            "raw_response": response,
        }

    score = int(data.get("overall_score", 0))
    checks = {
        "hook": {
            "score": int(data.get("hook", {}).get("score", 5)),
            "comment": str(data.get("hook", {}).get("comment", "")),
        },
        "point_of_view": {
            "score": int(data.get("point_of_view", {}).get("score", 5)),
            "comment": str(data.get("point_of_view", {}).get("comment", "")),
        },
        "storyline": {
            "score": int(data.get("storyline", {}).get("score", 5)),
            "comment": str(data.get("storyline", {}).get("comment", "")),
        },
        "technical_depth": {
            "score": int(data.get("technical_depth", {}).get("score", 5)),
            "comment": str(data.get("technical_depth", {}).get("comment", "")),
        },
        "wechat_readability": {
            "score": int(data.get("wechat_readability", {}).get("score", 5)),
            "comment": str(data.get("wechat_readability", {}).get("comment", "")),
        },
    }
    suggestions = [str(s) for s in data.get("suggestions", [])]
    verdict = str(data.get("verdict", "FAIL")).upper()
    passed = verdict == "PASS" and score >= 75

    return {
        "score": max(0, min(100, score)),
        "checks": checks,
        "suggestions": suggestions,
        "passed": passed,
        "raw_response": response,
    }


def verify_article(
    path: Path,
    deepread_item: dict | None = None,
    reviewer: object | None = None,
) -> dict:
    """验证单篇 Markdown 文章。

    可通过 import 直接调用，返回包含 passed/errors/warnings/score 等字段的 dict。

    硬性规则检查（文件名、字数、禁止词等）仍然用规则；
    内容质量评估交给独立的审稿 Agent。

    Args:
        path: 文章文件路径
        deepread_item: deepread 中的对应条目
        reviewer: 审稿 Agent（LLM provider 实例）。如果为 None 则自动创建
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        return {
            "file": str(path),
            "passed": False,
            "errors": [f"文件不存在: {path}"],
            "warnings": [],
            "score": 0,
            "checks": {},
            "rewrite_suggestions": [],
        }

    # ---- 硬性规则检查 ----
    if path.suffix.lower() != ".md":
        errors.append("文件扩展名必须是 .md")

    if path.parent.name != "daily_paper":
        warnings.append("文章建议放在 daily_paper/ 文件夹")

    if not re.match(r"^\d{4}-\d{2}-\d{2}-.+\.md$", path.name):
        errors.append("文件名必须符合 YYYY-MM-DD-文章标题.md")

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        errors.append("Markdown 内容为空")

    h1_matches = re.findall(r"^#\s+(.+)$", text, flags=re.MULTILINE)
    if not text.startswith("# "):
        errors.append("Markdown 必须以一级标题开头")
    if len(h1_matches) != 1:
        errors.append(f"Markdown 必须且只能有一个一级标题，当前为 {len(h1_matches)} 个")
    title = h1_matches[0] if h1_matches else ""

    h2_count = len(re.findall(r"^##\s+", text, flags=re.MULTILINE))
    if h2_count < 3:
        errors.append(f"至少需要 3 个二级标题，当前为 {h2_count} 个")

    char_count = chinese_char_count(text)
    if char_count < 1000:
        errors.append(f"正文过短，中文字符数少于 1000: {char_count}")
    elif char_count < 1200:
        warnings.append(f"篇幅略短，中文字符数建议不少于 1200: {char_count}")
    elif char_count > 3000:
        warnings.append(f"篇幅偏长，中文字符数建议不超过 3000: {char_count}")

    for label, pattern in PROHIBITED_PATTERNS:
        if re.search(pattern, text):
            errors.append(f"最终 Markdown 出现禁止内容: {label}")

    for pattern in OVERUSED_TEMPLATE_PATTERNS:
        if re.search(pattern, text):
            errors.append(f"最终 Markdown 出现旧模板化表达: {pattern}")

    if deepread_item:
        source_url = str(deepread_item.get("url", ""))
        if "github.com/" in source_url.lower():
            top_block = "\n".join(text.splitlines()[:8])
            if source_url not in top_block:
                errors.append("GitHub 项目类文章必须在开头紧跟标题给出项目链接")
        plan = deepread_item.get("article_plan") or {}
        must_include = plan.get("must_include") or []
        if must_include:
            covered = covered_terms(text, must_include)
            min_required = min(3, len(must_include))
            if len(covered) < min_required:
                errors.append(
                    "文章没有覆盖足够的原文关键对象: "
                    f"{len(covered)}/{len(must_include)}，至少需要 {min_required}"
                )

    # ---- 确定文章类型 ----
    article_type = None
    if deepread_item:
        article_type = deepread_item.get("article_type")
    if not article_type:
        # 简单推断作为 fallback
        if "github.com/" in text.lower() or "仓库" in text or "项目链接" in text:
            article_type = "工具型"
        elif "趋势" in text or "信号" in text:
            article_type = "解读型"
        else:
            article_type = "主线型"

    # ---- 审稿 Agent 评估 ----
    source_title = str(deepread_item.get("title", "")) if deepread_item else ""
    source_url = str(deepread_item.get("url", "")) if deepread_item else ""

    if reviewer is None:
        reviewer = _create_reviewer_provider()

    if reviewer is not None:
        review_result = review_with_agent(
            article_text=text,
            article_type=article_type,
            title=title,
            source_title=source_title,
            source_url=source_url,
            reviewer=reviewer,
        )
        score = review_result["score"]
        checks = review_result["checks"]
        suggestions = review_result["suggestions"]
        agent_passed = review_result["passed"]
    else:
        # 没有审稿 Agent 时，用简单规则做 fallback 评分
        score = min(100, max(40, 60 + h2_count * 2))
        checks = {}
        suggestions = []
        agent_passed = True
        warnings.append("审稿 Agent 未配置（REVIEW_LLM_API_KEY / REVIEW_LLM_MODEL），使用简单评分")

    if score < 75:
        warnings.append(f"审稿评分低于发布门槛 75: {score}")
    elif score < 85:
        warnings.append(f"审稿评分可用但建议润色: {score}")

    passed = len(errors) == 0 and agent_passed

    return {
        "file": str(path),
        "article_type": article_type,
        "passed": passed,
        "errors": errors,
        "warnings": warnings,
        "score": score,
        "checks": checks,
        "rewrite_suggestions": suggestions,
    }


def collect_article_paths(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(target.glob("*.md"))
    return [target]


def print_result(result: dict, verbose: bool) -> None:
    print(f"\n{'=' * 60}")
    print(f"文章: {result['file']}")
    print(f"类型: {result.get('article_type') or '未知'}")
    print(f"评分: {result['score']} (审稿 Agent)")
    print(f"结果: {'PASS' if result['passed'] else 'FAIL'}")
    print(f"{'=' * 60}")

    for msg in result["errors"]:
        print(f"ERROR: {msg}")
    for msg in result["warnings"]:
        print(f"WARN: {msg}")

    if verbose and result.get("checks"):
        print("审稿细项:")
        for name, detail in result["checks"].items():
            if isinstance(detail, dict):
                print(f"  [{detail.get('score', '?')}/10] {name}: {detail.get('comment', '')}")
    if result.get("rewrite_suggestions"):
        print("修改建议:")
        for suggestion in result["rewrite_suggestions"]:
            print(f"  - {suggestion}")


def main() -> None:
    parser = argparse.ArgumentParser(description="验证 daily_paper Markdown 成稿质量（审稿 Agent 模式）。")
    parser.add_argument("target", help="Markdown 文件路径或 daily_paper 目录")
    parser.add_argument("--deepread", help="对应的 reports/YYYY-MM-DD.deepread.json")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    parser.add_argument("--verbose", action="store_true", help="打印细项评分")
    parser.add_argument("--no-agent", action="store_true", help="禁用审稿 Agent，使用简单规则评分")
    args = parser.parse_args()

    deepread_map = load_deepread(Path(args.deepread)) if args.deepread else {}
    paths = collect_article_paths(Path(args.target))

    reviewer = None
    if not args.no_agent:
        reviewer = _create_reviewer_provider()
        if reviewer:
            print(f"[审稿 Agent] 已启用: {reviewer.model}")
        else:
            print("[审稿 Agent] 未配置（REVIEW_LLM_API_KEY / REVIEW_LLM_MODEL），使用简单评分")

    results = []
    for path in paths:
        item = deepread_map.get(path.name)
        results.append(verify_article(path, item, reviewer=reviewer))

    if args.json:
        # JSON 输出时不包含 raw_response
        clean_results = []
        for r in results:
            r_clean = {k: v for k, v in r.items() if k != "raw_response"}
            clean_results.append(r_clean)
        print(json.dumps(clean_results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print_result(result, args.verbose)

    if all(result["passed"] for result in results):
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
