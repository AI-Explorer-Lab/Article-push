"""verify_article.py - daily_paper Markdown 成稿验证与编辑评分脚本"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


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

GENERIC_HEADING_PATTERNS = [
    r"^发生了什么",
    r"^这次发生了什么",
    r"^为什么重要",
    r"^为什么这件事值得",
    r"^为什么值得",
    r"^背后原因",
    r"^技术含义",
    r"^技术看点",
    r"^适合谁",
    r"^局限",
    r"^需要谨慎",
    r"^接下来",
    r"^后续观察",
    r"^它是什么",
    r"^它解决",
    r"^真实痛点",
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


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


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


def infer_article_type(text: str) -> str | None:
    if "它是什么" in text or "适合谁" in text or "技术看点" in text:
        return "工具型"
    if "为什么" in text and ("趋势" in text or "背后" in text):
        return "解读型"
    if "发生了什么" in text or "这次发生了什么" in text:
        return "主线型"
    return None


def has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def covered_terms(text: str, terms: list[str]) -> list[str]:
    covered: list[str] = []
    for term in terms:
        normalized = str(term).strip()
        if len(normalized) < 3:
            continue
        if normalized in text:
            covered.append(normalized)
    return covered


def verify_structure_by_type(text: str, article_type: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if article_type == "主线型":
        groups = {
            "事实切入": ["这次事件", "这次进展", "核心看点", "进入了", "开始参与", "解决了"],
            "为什么重要": ["为什么", "重要", "不只是", "值得"],
            "影响或改变": ["意味着", "影响", "改变", "进入"],
            "后续观察": ["接下来", "后续", "应该看", "值得看"],
        }
    elif article_type == "解读型":
        groups = {
            "信号边界": ["信号", "趋势", "边界", "观察"],
            "机制解释": ["原因", "因为", "本质", "机制", "接入方式", "成本结构", "流程"],
            "技术含义": ["技术", "SDK", "API", "MCP", "harness", "上下文"],
            "影响判断": ["影响", "竞争", "开发者", "行业", "接下来"],
        }
    elif article_type == "工具型":
        groups = {
            "工具定义": ["它是什么", "可以理解成", "定位"],
            "适合人群": ["适合谁", "如果你", "值得关注"],
            "技术看点": ["技术看点", "关键词", "我最关注"],
            "局限提醒": ["谨慎", "局限", "风险", "需要验证", "不能只看"],
        }
    else:
        errors.append(f"未知文章类型: {article_type}")
        return errors, warnings

    for label, keywords in groups.items():
        if not has_any(text, keywords):
            errors.append(f"{article_type} 缺少结构要素: {label}")

    return errors, warnings


def score_article(text: str, title: str, article_type: str | None) -> tuple[int, dict[str, dict], list[str]]:
    paragraphs = split_paragraphs(text)
    body = "\n\n".join(paragraphs[1:]) if len(paragraphs) > 1 else text
    intro = body[:240]

    scores = {
        "hook": 8,
        "point_of_view": 8,
        "storyline": 8,
        "technical_depth": 8,
        "wechat_readability": 8,
    }
    comments: dict[str, dict] = {}
    suggestions: list[str] = []

    first_sentence = re.split(r"[。！？\n]", intro.strip())[0]
    if not has_any(intro, ["关键", "问题", "但", "反而", "边界", "流程", "成本", "接入"]):
        scores["hook"] -= 3
        suggestions.append("开头缺少具体问题或判断，建议前 200 字内交代对象、动作和矛盾。")
    if intro.startswith("今天") or intro.startswith("有一类"):
        scores["hook"] -= 3
        suggestions.append("开头略像流水账，可以直接从冲突或判断切入。")
    if has_any(first_sentence, ["消息", "新闻", "项目"]) and not has_any(first_sentence, ["为什么", "问题", "边界", "成本", "流程"]):
        scores["hook"] -= 2
        suggestions.append("第一句话过于资讯化，建议用问题、矛盾或结论打开。")
    if "？" not in intro and "！" not in intro and not has_any(intro[:120], ["但", "反而", "边界", "流程"]):
        scores["hook"] -= 1

    if not has_any(text, ["我觉得", "本质", "关键", "分水岭", "问题在于", "取决于", "边界", "成本"]):
        scores["point_of_view"] -= 4
        suggestions.append("观点感偏弱，建议增加一句明确判断：这件事改动了哪段流程或暴露了什么边界。")
    if len(re.findall(r"值得|重要|关键", text)) > 14:
        scores["point_of_view"] -= 2
        suggestions.append("高频判断词偏多，建议用更具体的场景替代抽象判断。")
    if len(re.findall(r"这类|这种|这个方向|这件事", text)) > 10:
        scores["point_of_view"] -= 1
        suggestions.append("指代词偏多，文章容易变虚，建议补充更具体的对象和场景。")

    h2_count = len(re.findall(r"^##\s+", text, flags=re.MULTILINE))
    if h2_count < 4:
        scores["storyline"] -= 2
        suggestions.append("二级标题偏少，文章推进层次不够清楚。")
    if not has_any(text[-500:], ["接下来", "如果", "未来", "这才是", "所以"]):
        scores["storyline"] -= 2
        suggestions.append("结尾缺少收束判断或下一步观察。")
    headings = re.findall(r"^##\s+(.+)$", text, flags=re.MULTILINE)
    generic_headings = sum(
        1
        for heading in headings
        if any(re.search(pattern, heading) for pattern in GENERIC_HEADING_PATTERNS)
    )
    if generic_headings >= 3:
        scores["storyline"] -= 2
        suggestions.append("小标题模板感较重，建议让标题带出具体判断，而不是只标功能区块。")
    elif generic_headings > 0:
        scores["storyline"] -= generic_headings
        suggestions.append("部分二级标题暴露了写作结构标签，建议把“发生了什么/为什么重要”等融入具体判断。")

    technical_terms = [
        "Agent",
        "MCP",
        "harness",
        "Context Engineering",
        "Harness Engineering",
        "SDK",
        "API",
        "RAG",
        "CI",
        "模型",
        "工具",
        "上下文",
        "评测",
        "工作流",
        "权限",
        "路由",
    ]
    term_hits = sum(1 for term in technical_terms if term in text)
    if term_hits < 4:
        scores["technical_depth"] -= 4
        suggestions.append("技术密度不足，建议补充具体工程概念或开发者场景。")
    if not has_any(text, ["比如", "举个", "这就像", "可以理解成"]):
        scores["technical_depth"] -= 3
        suggestions.append("缺少具体例子或类比，读者理解成本会偏高。")
    if not has_any(text, ["失败", "权限", "测试", "部署", "审查", "成本", "状态", "日志", "边界", "工作流"]):
        scores["technical_depth"] -= 2
        suggestions.append("缺少真实工程约束，容易像概念解读而不是技术文章。")

    long_paragraphs = [p for p in paragraphs if chinese_char_count(p) > 180]
    if long_paragraphs:
        scores["wechat_readability"] -= min(4, len(long_paragraphs))
        suggestions.append("存在过长段落，建议拆短以适应公众号阅读节奏。")

    prose_paragraphs = [
        p
        for p in paragraphs
        if not p.startswith("#")
        and not p.startswith("项目链接")
        and not re.match(r"^[-*]\s+", p)
    ]
    short_paragraphs = [
        p
        for p in prose_paragraphs
        if chinese_char_count(p) < 45 and len(re.findall(r"[。！？]", p)) <= 1
    ]
    if prose_paragraphs and len(short_paragraphs) / len(prose_paragraphs) > 0.45:
        scores["wechat_readability"] -= 3
        suggestions.append("超短段落比例偏高，建议把连续的一两句话合并成更完整的自然段。")

    if chinese_char_count(text) < 1000:
        scores["wechat_readability"] -= 5
        suggestions.append("篇幅过短，通常还没形成完整论证。")
    elif chinese_char_count(text) < 1200:
        scores["wechat_readability"] -= 3
        suggestions.append("篇幅偏短，可能还没充分展开。")
    if chinese_char_count(text) > 3000:
        scores["wechat_readability"] -= 2
        suggestions.append("篇幅偏长，建议压缩重复段落。")

    if title and title.strip("# ") in text.replace(title, "", 1):
        scores["hook"] -= 1

    for key, score in scores.items():
        scores[key] = max(0, min(10, score))

    comments["hook"] = {
        "score": scores["hook"],
        "comment": "开头钩子越清楚，越能避免文章像普通资讯摘要。",
    }
    comments["point_of_view"] = {
        "score": scores["point_of_view"],
        "comment": "公众号文章需要有明确判断，而不只是解释新闻。",
    }
    comments["storyline"] = {
        "score": scores["storyline"],
        "comment": "结构需要有推进感：事实、解释、影响、观察。",
    }
    comments["technical_depth"] = {
        "score": scores["technical_depth"],
        "comment": "技术深度来自具体概念、工程场景和边界条件。",
    }
    comments["wechat_readability"] = {
        "score": scores["wechat_readability"],
        "comment": "短段落、明确小标题和具体例子会提升可读性。",
    }

    total = round(sum(scores.values()) * 2)
    if article_type not in VALID_ARTICLE_TYPES:
        total -= 5
        suggestions.append("未能确定文章类型，建议从 deepread 中传入对应 article_type。")
    return max(0, min(100, total)), comments, suggestions


def verify_article(path: Path, deepread_item: dict | None = None) -> dict:
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

    h2_titles = re.findall(r"^##\s+(.+)$", text, flags=re.MULTILINE)
    exposed_headings = [
        heading
        for heading in h2_titles
        if any(re.search(pattern, heading) for pattern in GENERIC_HEADING_PATTERNS)
    ]
    if exposed_headings:
        warnings.append(
            "二级标题不应直接暴露结构标签，建议改成具体判断: "
            + "；".join(exposed_headings)
        )

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

    article_type = None
    if deepread_item:
        article_type = deepread_item.get("article_type")
    if not article_type:
        article_type = infer_article_type(text)

    if article_type:
        struct_errors, struct_warnings = verify_structure_by_type(text, article_type)
        errors.extend(struct_errors)
        warnings.extend(struct_warnings)
    else:
        warnings.append("无法推断文章类型，跳过类别结构校验")

    score, checks, suggestions = score_article(text, title, article_type)
    if score < 75:
        warnings.append(f"编辑评分低于发布门槛 75: {score}")
    elif score < 85:
        warnings.append(f"编辑评分可用但建议润色: {score}")

    return {
        "file": str(path),
        "article_type": article_type,
        "passed": len(errors) == 0 and score >= 75,
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
    print(f"评分: {result['score']}")
    print(f"结果: {'PASS' if result['passed'] else 'FAIL'}")
    print(f"{'=' * 60}")

    for msg in result["errors"]:
        print(f"ERROR: {msg}")
    for msg in result["warnings"]:
        print(f"WARN: {msg}")

    if verbose:
        for name, detail in result["checks"].items():
            print(f"- {name}: {detail['score']}/10 - {detail['comment']}")
    if result["rewrite_suggestions"]:
        print("重写建议:")
        for suggestion in result["rewrite_suggestions"]:
            print(f"- {suggestion}")


def main() -> None:
    parser = argparse.ArgumentParser(description="验证 daily_paper Markdown 成稿质量。")
    parser.add_argument("target", help="Markdown 文件路径或 daily_paper 目录")
    parser.add_argument("--deepread", help="对应的 reports/YYYY-MM-DD.deepread.json")
    parser.add_argument("--json", action="store_true", help="输出 JSON 结果")
    parser.add_argument("--verbose", action="store_true", help="打印细项评分")
    args = parser.parse_args()

    deepread_map = load_deepread(Path(args.deepread)) if args.deepread else {}
    paths = collect_article_paths(Path(args.target))
    results = []
    for path in paths:
        item = deepread_map.get(path.name)
        results.append(verify_article(path, item))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print_result(result, args.verbose)

    if all(result["passed"] for result in results):
        sys.exit(0)
    sys.exit(1)


if __name__ == "__main__":
    main()
