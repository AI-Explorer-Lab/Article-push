"""src/core/writer.py - 文章写作模块。

架构（精简版 v2.2）：
- 直接从 agent.py 的 report JSON 读取 items
- 用 AI 阅读原文判断质量（好不好）
- 好的就生成 MD 文章，不好就跳过下一个
- 生成初稿后保留上下文记忆（original_text），等审稿 Agent 通过后再丢弃
- 审稿不通过时，用审稿建议 + 原文上下文让 LLM 修改文章
- 目标产出 5 篇文章，GitHub 最多 2 篇
- 文章类型推断、标题生成、输出路径生成等逻辑内嵌在本模块
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from src.common.utils import chinese_char_count, load_json, slugify, write_json
from src.core.context import (
    extract_required_terms,
    fact_or_fallback,
    fetch_original_context,
    source_subject,
)
from src.infrastructure.llm_client import OpenAICompatibleProvider


# ---------------------------------------------------------------------------
# 文章类型风格契约（从 deepread.py 迁移）
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 工具函数（从 deepread.py 迁移）
# ---------------------------------------------------------------------------

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
    """根据条目和类型生成文章标题。"""
    title = str(item.get("title", "")).strip(" .")
    compact = re.sub(r"GitHub 项目更新：", "", title)
    compact = compact.replace("！", "").replace("？", "")
    return compact[:50]


def _source_subject(item: dict) -> str:
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

    hint = slugify(_source_subject(item), max_len=18)
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


# ---------------------------------------------------------------------------
# AI 质量评估
# ---------------------------------------------------------------------------

def evaluate_article_quality_with_ai(
    item: dict,
    original_text: str,
    llm_provider: OpenAICompatibleProvider,
) -> tuple[bool, str]:
    """用 AI 评估文章质量。

    返回 (是否通过, 评估理由)。
    评估维度：是否有实质内容、是否与关注领域相关、是否适合写成公众号文章。
    """
    title = item.get("title", "")
    source = item.get("source", "")
    url = item.get("url", "")

    prompt = [
        {
            "role": "system",
            "content": (
                "你是严谨的技术编辑。你需要评估一篇文章是否值得写成公众号推文。\n"
                "评估标准：\n"
                "1. 有实质内容（不是标题党、不是纯营销、不是空泛介绍）\n"
                "2. 与 AI/LLM/Agent/MCP/上下文工程/推理优化/多模态/编程 等领域相关\n"
                "3. 有可写的技术看点、工程启发或趋势判断\n"
                "4. 不是垃圾信息、乱码、或纯导航页面\n\n"
                "只回答 PASS 或 SKIP，然后给一句简短理由。格式：PASS: 理由 或 SKIP: 理由"
            ),
        },
        {
            "role": "user",
            "content": (
                f"标题：{title}\n"
                f"来源：{source}\n"
                f"URL：{url}\n\n"
                f"原文内容（前 3000 字）：\n{original_text[:3000]}"
            ),
        },
    ]

    try:
        response = llm_provider.chat(prompt, temperature=0.2, max_tokens=200)
        response = response.strip()
        if response.upper().startswith("PASS"):
            return True, response
        else:
            return False, response
    except Exception as exc:
        # AI 评估失败时，默认通过（避免因为网络问题漏掉好内容）
        return True, f"AI评估异常({exc})，默认通过"


# ---------------------------------------------------------------------------
# LLM 文章生成
# ---------------------------------------------------------------------------

def build_llm_writer_prompt(item: dict, original_text: str) -> list[dict]:
    """构建 LLM 写作 prompt。"""
    plan = item.get("article_plan") or {}
    article_type = item.get("article_type", "")
    contract = STYLE_CONTRACTS.get(article_type, {})
    must_include = plan.get("must_include") or []

    github_note = ""
    if article_type == "工具型" and "github.com/" in str(item.get("url", "")).lower():
        github_note = f"- 标题下方保留项目链接：{item.get('url')}\n"

    # 每种类型的风格引导（不用 checklist 强制要求出现特定关键词）
    type_guidance = {
        "主线型": (
            "这是主线型文章：围绕一条具体事件推进。\n"
            "先讲清楚原文发生了什么（谁、做了什么、结果是什么），"
            "再解释这件事改变了什么，最后落到后续应该观察什么。\n"
            "不要写成「先说概念再讲意义」的模板结构；"
            "让事实本身驱动叙述节奏，判断放在事实之后。\n"
        ),
        "解读型": (
            "这是解读型文章：围绕一个技术信号或趋势展开。\n"
            "先用原文中的具体事实说明信号是什么、边界在哪里，"
            "再解释这个变化背后的机制原因，最后判断对开发者或行业意味着什么。\n"
            "不要拔高成确定趋势，判断要克制，留有余地。\n"
        ),
        "工具型": (
            "这是工具型文章：围绕一个工具、项目或产品展开。\n"
            "先说清楚它解决什么问题、怎么用的，"
            "再说它的技术设计和工程取舍有什么值得注意的，"
            "最后诚实地说它的局限和需要验证的地方。\n"
            "不写宣传腔，多写使用场景和失败路径。\n"
        ),
    }.get(article_type, "")

    system = (
        "你是一名技术公众号编辑，文章风格贴近「技术博客 + 行业观察」。\n"
        "你只能基于用户提供的原文全文写作，不得联网，不得添加原文没有的事实。\n"
        "写作要求：\n"
        "- 有自己的判断和视角，不是原文的摘要或翻译。\n"
        "- 段落有起承转合：一段话里的事实、解释、判断要自然衔接，不要各说各的。\n"
        "- 二级标题要像人写的：用原文中的具体信息做标题，不要写成「XX的意义」「XX的影响」这种泛泛的概括句。\n"
        "- 不要用「首先…其次…最后」「一方面…另一方面」「总而言之」这种套话连接词。\n"
        "- 不要出现「在当今时代」「随着AI的发展」「众所周知」这类空洞开头。\n"
    )

    user = f"""请基于下面的原文全文，写一篇原创中文 Markdown 文章。

{type_guidance}
硬性规则：
- 只能使用原文中的事实；允许重组、解释、压缩和改写，但不允许新增事实。
- 如果原文信息不足，必须降低判断强度，不要脑补细节。
- Markdown 以一级标题开头（只能有一个一级标题），一级标题要像一篇独立文章的标题，而不是「关于XX的几点思考」这种。
- 至少 3 个二级标题，每个二级标题要包含原文中的具体信息（人名、项目名、数据、技术术语等），不要写成空泛的概括句。
- 中文正文 1300-1800 字。
- 段落 2-4 句为主，单句不独立成段。
- 至少嵌入一个自然的具体例子或类比。
- 不写归因区块（「参考来源」「据XX报道」等）。
- 不写「真正值得看」「背后的技术信号」「这条动态值得拆开看」等模板腔。
{github_note}
核心判断（仅供参考，不要照抄）：{plan.get("core_claim", "")}
原标题（仅供参考）：{item.get("title", "")}

原文全文：
{original_text[:120000]}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def polish_llm_article(content: str, article_type: str) -> str:
    """对 LLM 生成的文章进行风格润色。

    只做去模板腔的文本替换，不再强制改写二级标题。
    标题的最终形态由 LLM 根据原文内容自由生成。
    """
    replacements = {
        "真正值得看的不是": "需要关注的不是",
        "真正值得看的，不是": "需要关注的，不是",
        "真正值得看": "需要关注",
        "真正考验": "关键考验",
        "背后的技术信号": "其中的技术信号",
        "这条动态值得拆开看": "这条动态适合按工程链路拆开看",
        "不只要看功能": "还要看接入与验证",
        "不能只看功能": "需要同时看接入与验证",
        "这件事到底改变了什么": "它改动了哪段流程",
        "最后落回一个简单问题": "最后要回到可复用性",
        "放回日常工作里": "放到具体团队流程里",
        "上下文来源：": "上下文入口：",
        "三类上下文来源：": "三类上下文入口：",
        "在当今时代，": "",
        "随着AI的快速发展，": "",
        "众所周知，": "",
        "值得注意的是，": "",
        "值得一提的是，": "",
        "首先，": "",
        "其次，": "",
        "最后，": "",
        "综上所述，": "",
        "总而言之，": "",
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    content = re.sub(
        r"真正([^。！？\n]{0,20})不是([^。！？\n]{0,60})而是",
        r"关键\1不在于\2，而在于",
        content,
    )
    return wrap_long_paragraphs(content)


def wrap_long_paragraphs(article: str) -> str:
    """将过长段落按句拆分。"""
    wrapped: list[str] = []
    for paragraph in article.split("\n\n"):
        stripped = paragraph.strip()
        if not stripped or stripped.startswith("#") or chinese_char_count(stripped) <= 170:
            wrapped.append(paragraph)
            continue
        sentences = re.split(r"(?<=[。！？])", stripped)
        current = ""
        chunks: list[str] = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if current and chinese_char_count(current + sentence) > 150:
                chunks.append(current.strip())
                current = sentence
            else:
                current += sentence
        if current:
            chunks.append(current.strip())
        wrapped.append("\n\n".join(chunks))
    return "\n\n".join(wrapped) + "\n"


def build_intro(article_type: str, subject: str, claim: str, facts: list[str]) -> str:
    """构建导语段落（规则写作器 fallback）。"""
    first_fact = fact_or_fallback(facts, 0, f"原文围绕「{subject}」给出了一个具体对象和动作。")
    if article_type == "工具型":
        return (
            f"工具型项目先看定位，再看接入成本。围绕「{subject}」，需要问清楚它解决什么任务，"
            f"又把哪些工程边界暴露出来。{first_fact} 这篇文章会把它放回真实使用场景里看："
            "它能否安装、能否接进任务、能否留下状态，比一句能力描述更有信息量。"
        )
    if article_type == "解读型":
        return (
            f"这条动态可以作为一个技术信号来读，但边界要先说清楚。{first_fact} "
            f"我的判断是：{claim} 这个判断需要回到原文里的具体事实，不能只靠概念拔高。"
            "先看事实怎么发生，再看它可能改写哪一段开发者流程。"
        )
    return (
        f"这次事件先按事实读，再按流程读。{first_fact} 需要追问的是：它具体改变了哪段流程，"
        f"哪些环节仍然需要人来判断，以及它为什么会被放到今天的 AI Agent 语境里讨论。"
        "如果这几个问题讲不清楚，文章就只是在复述标题。"
    )


# ---------------------------------------------------------------------------
# 单篇文章生成（含 AI 质量评估）
# ---------------------------------------------------------------------------

def build_required_terms_paragraph(item: dict) -> str:
    """把 deepread 要求的关键对象自然融入文章。"""
    plan = item.get("article_plan") or {}
    picked: list[str] = []
    for raw in plan.get("must_include") or []:
        term = str(raw).strip()
        if len(term) < 3 or term in picked:
            continue
        picked.append(term)
        if len(picked) >= 4:
            break
    if not picked:
        return ""

    joined = "、".join(picked)
    if item.get("article_type") == "工具型":
        return (
            f"先把原文里最关键的几个对象摆出来：{joined}。这些词不是为了堆标签，"
            "而是为了确认项目定义、接入位置和失败处理到底落在哪一层。"
        )
    return (
        f"先把原文里最关键的几个对象摆出来：{joined}。把这些对象说清楚，后面的判断才不会飘在标题和情绪上，"
        "也更容易看出这次变化是确实进入了工程链路，还是停留在宣传层。"
    )


def rewrite_one_article_with_llm(
    item: dict,
    context: dict,
    llm_provider: OpenAICompatibleProvider,
) -> str:
    """使用 LLM 生成单篇文章。"""
    original_text = str(context.get("original_text") or "")
    if len(original_text) < 300:
        raise RuntimeError(
            f"原文抓取不足（{len(original_text)} 字符），无法生成文章: {item.get('title', '')}"
        )

    article = llm_provider.chat(
        build_llm_writer_prompt(item, original_text),
    )
    article = polish_llm_article(article, str(item.get("article_type", "")))
    return article


def rewrite_one_article_rules(
    item: dict,
    context: dict,
) -> str:
    """使用规则模板生成文章（fallback 方案）。"""
    article_type = item["article_type"]
    contract = STYLE_CONTRACTS[article_type]
    plan = item["article_plan"]
    facts = context["facts"]
    title = str(plan["title"])
    subject = context["subject"]
    claim = str(plan.get("core_claim", ""))
    required_terms = build_required_terms_paragraph(item)

    project_line = ""
    if article_type == "工具型" and "github.com/" in str(item.get("url", "")).lower():
        project_line = f"\n项目链接：{item['url']}\n"

    sections = contract.get("sections", contract.get("must_answer", ["事实", "判断", "影响", "后续"]))
    intro = build_intro(article_type, subject, claim, facts)

    body = [
        f"# {title}",
        "",
        intro + project_line,
        "",
        required_terms,
        "",
    ]
    for i, section in enumerate(sections[:4]):
        body.extend([
            f"## {section}",
            "",
            _build_section(article_type, facts, claim, i, contract),
            "",
        ])
    article = "\n".join(body)
    article = _expand_if_short(article, article_type, subject, claim)
    article = wrap_long_paragraphs(article)
    return article


def _build_section(article_type: str, facts: list[str], claim: str, idx: int, contract: dict) -> str:
    """构建文章段落。"""
    templates = {
        "工具型": [
            lambda: f"{fact_or_fallback(facts, 0, '先看原文给出的对象。')} {fact_or_fallback(facts, 1, '再看它把任务、工具、状态和使用者放进同一个流程里了吗。')}",
            lambda: f"{fact_or_fallback(facts, 2, '原文里的关键细节，通常藏在流程描述和能力边界里。')} {fact_or_fallback(facts, 3, '这些细节决定它是一次演示，还是可以继续被复用的方法。')}",
            lambda: f"{fact_or_fallback(facts, 4, '从使用者视角看，影响工作的是能不能把结果接进下一步。')} {fact_or_fallback(facts, 5, '如果缺少验证和复盘，再漂亮的结果也很难变成稳定流程。')}",
            lambda: f"{fact_or_fallback(facts, 6, '原文没有完全回答的问题同样值得保留。')} {contract.get('tone', '')}",
        ],
        "解读型": [
            lambda: f"{fact_or_fallback(facts, 0, '原文给出的第一层信息是具体进展。')} {fact_or_fallback(facts, 1, '这些事实指向一个判断：{claim}。')}",
            lambda: f"{fact_or_fallback(facts, 2, '原文里的关键细节，通常藏在流程描述和能力边界里。')} {fact_or_fallback(facts, 3, '趋势判断不能脱离这些细节。')}",
            lambda: f"{fact_or_fallback(facts, 4, '从使用者视角看，影响工作的是能不能把结果接进下一步。')} {fact_or_fallback(facts, 5, '对开发者来说，变化往往不是突然发生在模型分数上。')}",
            lambda: f"{fact_or_fallback(facts, 6, '原文没有完全回答的问题同样值得保留。')} {contract.get('tone', '')}",
        ],
        "主线型": [
            lambda: f"{fact_or_fallback(facts, 0, '先看事实层。')} {fact_or_fallback(facts, 1, '如果只把它当成一条热闹新闻，就会漏掉流程问题。')}",
            lambda: f"{fact_or_fallback(facts, 2, '原文里的关键细节。')} {fact_or_fallback(facts, 3, '主线型文章不能只复述标题。')}",
            lambda: f"{fact_or_fallback(facts, 4, '从使用者视角看。')} {fact_or_fallback(facts, 5, '这也是这类事件的现实影响。')}",
            lambda: f"{fact_or_fallback(facts, 6, '原文没有完全回答的问题同样值得保留。')} {contract.get('tone', '')}",
        ],
    }
    fn = templates.get(article_type, templates["主线型"])[min(idx, 3)]
    return fn()


def _expand_if_short(article: str, article_type: str, subject: str, claim: str) -> str:
    """如果文章过短，补充编辑判断段落。"""
    if chinese_char_count(article) >= 1200:
        return article
    addition = (
        f"\n补充一层编辑判断：围绕「{subject}」写作时，质量来自事实、判断和边界的配合。"
        "事实负责把对象说准，判断负责指出变化方向，边界负责避免把单条材料拔高成结论。"
        "如果原文能提供足够细节，文章就多还原动作和流程；如果原文细节有限，文章就明确观察口径。"
    )
    return article.rstrip() + "\n" + addition


def _expand_if_short_v2(article: str, article_type: str, subject: str, claim: str) -> str:
    """补一个更长、更像正式推文的兜底版本。"""
    if chinese_char_count(article) >= 1200:
        return article

    common_addition = (
        f"补充一层编辑判断：围绕「{subject}」写作时，质量来自事实、判断和边界的配合。"
        f"这篇文章想说明的核心，不只是事件本身，而是{claim or '它把哪一段工作流改成了可重复的工程动作'}。"
        "如果只停在标题和热度，读者看到的是一条资讯；只有把对象、动作、约束和验证点摆在一起，文章才会变成真正可复用的判断。"
        "\n\n"
        "举个例子，团队评估一条新能力时，通常不会先问“酷不酷”，而会先问能不能接入现有流程、失败时谁来兜底、日志和状态是否可追踪。"
        "这也是为什么同样一条进展，有的内容适合做演示，有的内容才适合进入生产。"
        "把这些工程问题写出来，文章的技术密度和可读性都会明显提升。"
    )
    storyline_addition = (
        "\n\n后续观察同样重要。接下来真正值得看的，不是它还能讲出多少新概念，而是它有没有把规划、执行、反馈和复盘串成闭环。"
        "如果后续材料继续补出部署细节、接口约束和真实案例，这条线索的判断会更稳；如果没有，这篇文章也应该把保留意见写清楚。"
    )
    tool_addition = (
        "\n\n技术看点不能只写成功路径，更要写清楚它如何处理状态、权限、失败恢复和人工审查。"
        "如果这些位置没有交代，再顺手的工具也容易停在 demo 阶段。"
        "局限提醒也要提前说透：例如依赖环境是否稳定、接入成本是否可控、关键链路是不是仍然需要人工确认，这些都需要验证。"
    )
    addition = common_addition + storyline_addition
    if article_type == "工具型":
        addition += tool_addition
    return article.rstrip() + "\n\n" + addition


# ---------------------------------------------------------------------------
# 主入口：逐篇评估 + 生成
# ---------------------------------------------------------------------------

def generate_articles(
    report_path: Path,
    root: Path,
    overwrite: bool = False,
    use_llm_writer: bool = True,
    llm_provider: OpenAICompatibleProvider | None = None,
    llm_rewrite_attempts: int = 0,
    llm_max_original_chars: int = 120000,
) -> tuple[list[dict], dict[str, dict]]:
    """从 report JSON 逐篇评估并生成文章。

    流程：
    1. 直接读取 agent.py 产出的 report JSON（reports/{date}.json）
    2. 对每篇候选：推断文章类型 → 生成输出路径 → 抓取原文 → AI 评估质量
    3. 好的就生成 MD 文章，不好的跳过
    4. 生成初稿后保留上下文（original_text），等审稿 Agent 通过后再丢弃
    5. 最多产出 5 篇文章，GitHub 最多 2 篇

    Returns:
        (article_states, contexts): article_states 是状态列表，
        contexts 是 {output_filename: context_dict} 的映射，
        调用方在审稿全部通过后调用 discard_contexts() 清理。
    """
    data = load_json(report_path)
    paper_dir = root / "daily_paper"
    states_dir = root / "states"
    paper_dir.mkdir(parents=True, exist_ok=True)
    states_dir.mkdir(parents=True, exist_ok=True)

    items = data.get("items", [])
    article_states: list[dict] = []
    contexts: dict[str, dict] = {}  # {output_filename: context_dict}

    github_count = 0
    generated_count = 0
    MAX_GITHUB = 2
    TARGET_ARTICLES = 5
    used_outputs: set[str] = set()

    for item in items:
        url = item.get("url", "")
        is_github = "github.com/" in url.lower()

        # GitHub 最多 2 篇
        if is_github and github_count >= MAX_GITHUB:
            print(f"  SKIP (GitHub 已达上限): {item.get('title', '')[:50]}")
            article_states.append({
                "date": data.get("date"),
                "title": item.get("title", ""),
                "url": url,
                "stage": "skipped_github_limit",
            })
            continue

        # 推断文章类型并生成输出路径
        article_type = infer_article_type(item)
        title = article_title(item, article_type)
        output_file = unique_output_file(data["date"], title, item, used_outputs)
        output = root / output_file

        base_state = {
            "date": data.get("date"),
            "title": item.get("title", ""),
            "url": url,
            "article_type": article_type,
            "output_file": output_file,
            "context_scope": "single_article",
            "memory_policy": "retain_until_review_pass",
            "stage": "pending",
        }

        if output.exists() and not overwrite:
            print(f"  KEEP existing: {output.relative_to(root)}")
            article_states.append({**base_state, "stage": "kept_existing", "output_exists": True})
            if is_github:
                github_count += 1
            generated_count += 1
            continue

        # Step 1: 构建增强的 item（注入 article_type 和 article_plan，供 context/writer 使用）
        source_text = " ".join(
            str(item.get(key, "")) for key in ["title", "summary", "insight", "category"]
        )
        must_include = extract_required_terms(source_text, item.get("url", ""))
        enhanced_item = {
            **item,
            "article_type": article_type,
            "selection_reason": selection_reason(item, article_type),
            "article_plan": {
                "title": title,
                "core_claim": core_claim(item, article_type),
                "must_include": must_include,
                "original_summary": item.get("summary", ""),
                "original_insight": item.get("insight", ""),
            },
        }

        # Step 2: 抓取原文上下文
        print(f"\n  [READ] {item.get('title', '')[:50]}...")
        context = fetch_original_context(enhanced_item)
        original_text = str(context.get("original_text") or "")

        if len(original_text) < 300:
            print(f"  [SKIP] 原文抓取不足，跳过")
            article_states.append({**base_state, "stage": "failed_fetch", "error": "原文不足300字"})
            context.pop("original_text", None)
            del context
            continue

        # Step 3: AI 评估质量（如果用 LLM writer）
        if use_llm_writer and llm_provider:
            print(f"  [EVAL] AI 评估质量中...")
            passed, reason = evaluate_article_quality_with_ai(item, original_text, llm_provider)
            print(f"  [{'PASS' if passed else 'SKIP'}] {reason[:100]}")
            if not passed:
                article_states.append({
                    **base_state,
                    "stage": "skipped_quality",
                    "quality_reason": reason,
                })
                context.pop("original_text", None)
                del context
                continue

        # Step 4: 生成文章
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            if use_llm_writer and llm_provider:
                print(f"  [WRITE] LLM 生成文章中...")
                text = rewrite_one_article_with_llm(enhanced_item, context, llm_provider)
            else:
                print(f"  [WRITE] 规则模板生成文章中...")
                text = rewrite_one_article_rules(enhanced_item, context)

            output.write_text(text, encoding="utf-8")
            print(f"  [DONE] 生成完成: {output.relative_to(root)} ({chinese_char_count(text)} 字)")

            if is_github:
                github_count += 1
            generated_count += 1

            article_states.append({
                **base_state,
                "stage": "drafted",
                "output_exists": True,
                "char_count": chinese_char_count(text),
                "writer": "llm" if (use_llm_writer and llm_provider) else "rule_based",
            })
            # 保留上下文，等审稿通过后再丢弃
            contexts[output.name] = context
        except Exception as exc:
            print(f"  [FAIL] 生成失败: {exc}")
            article_states.append({
                **base_state,
                "stage": "failed_generate",
                "error": str(exc),
            })
            context.pop("original_text", None)
            del context

    print(f"\n[STATS] 生成统计: {generated_count} 篇 (GitHub: {github_count}, 其他: {generated_count - github_count})")
    print(f"[MEMORY] 保留 {len(contexts)} 篇文章的上下文，等待审稿")

    # 暂不写入状态文件——等审稿循环完成后再写最终状态
    return article_states, contexts


def discard_contexts(contexts: dict[str, dict]) -> None:
    """清理所有保留的上下文记忆（在审稿全部通过后调用）。"""
    for name, ctx in contexts.items():
        ctx.pop("original_text", None)
    contexts.clear()
    print(f"[MEMORY] 已丢弃所有上下文记忆")


def revise_article_with_review_feedback(
    article_text: str,
    original_text: str,
    item: dict,
    review_suggestions: list[str],
    review_checks: dict,
    llm_provider: OpenAICompatibleProvider,
    strategy: dict | None = None,
) -> str:
    """根据审稿 Agent 的反馈修改文章。

    Args:
        article_text: 当前文章全文
        original_text: 原文全文
        item: 条目信息（含 article_type、title 等）
        review_suggestions: 审稿 Agent 的修改建议列表
        review_checks: 审稿 Agent 的五维度评分详情
        llm_provider: 写作 LLM provider
        strategy: 修改策略 {"label": str, "temperature": float, "description": str}
                  不同轮次使用不同策略强度，防止温和修补无效果

    Returns:
        修改后的 Markdown 文章
    """
    article_type = item.get("article_type", "")
    title = item.get("title", "")
    plan = item.get("article_plan") or {}

    # 使用策略参数（默认温和策略）
    if strategy is None:
        strategy = {"label": "gentle", "temperature": 0.5, "description": "温和修改"}

    # 把审稿建议格式化为具体指令
    suggestions_text = "\n".join(f"- {s}" for s in review_suggestions)
    checks_text = ""
    for name, detail in review_checks.items():
        if isinstance(detail, dict):
            checks_text += f"- {name}: {detail.get('score', '?')}/10 — {detail.get('comment', '')}\n"

    # 根据策略强度调整 system prompt
    strategy_label = strategy.get("label", "gentle")

    if "aggressive" in strategy_label:
        system = (
            "你是一名技术公众号编辑。审稿人已经多次审读了你的文章并反复给出了修改建议，"
            "文章仍存在明显问题。\n"
            "这次需要做大幅度修改，不要只改几个词：\n"
            "- 可以重写开头，用完全不同的切入点\n"
            "- 可以重组文章结构，调整二级标题和段落顺序\n"
            "- 如果某些段落实在改不好，可以直接重写整段\n"
            "- 模板化的表达全部替换成具体、自然的写法\n"
            "- 不要删除原文中有价值的事实信息\n"
            "- 保持文章风格：技术博客 + 行业观察\n"
        )
    elif "moderate" in strategy_label:
        system = (
            "你是一名技术公众号编辑。审稿人已经读完了你的文章并给出了修改建议。\n"
            "这次需要做较大幅度的修改，不要只改几个词：\n"
            "- 如果审稿人说开头不够抓人，就重写开头，用具体的事实或矛盾切入\n"
            "- 如果审稿人说观点太弱，就加入自己的明确判断（但判断要克制有分寸）\n"
            "- 如果审稿人说结构模板化，就重新组织段落顺序和二级标题\n"
            "- 如果审稿人说技术深度不够，就补充原文中的具体技术细节\n"
            "- 如果审稿人说语言像机翻，就重写成自然的中文表达\n"
            "- 不要删除原文中有价值的事实信息\n"
            "- 保持文章风格：技术博客 + 行业观察\n"
        )
    else:
        system = (
            "你是一名技术公众号编辑。审稿人已经读完了你的文章并给出了修改建议。\n"
            "请根据审稿建议逐条修改文章，不要敷衍，不要只改几个词。\n"
            "修改原则：\n"
            "- 如果审稿人说开头不够抓人，就重写开头，用具体的事实或矛盾切入\n"
            "- 如果审稿人说观点太弱，就加入自己的明确判断（但判断要克制有分寸）\n"
            "- 如果审稿人说结构模板化，就重新组织段落顺序和二级标题\n"
            "- 如果审稿人说技术深度不够，就补充原文中的具体技术细节\n"
            "- 如果审稿人说语言像机翻，就重写成自然的中文表达\n"
            "- 不要删除原文中有价值的事实信息\n"
            "- 保持文章风格：技术博客 + 行业观察\n"
        )

    user = f"""以下是审稿人对你文章的评价和修改建议，请据此修改文章。

修改策略：{strategy.get('description', '根据审稿建议修改')}

【文章类型】{article_type}
【原标题】{title}

【审稿细项评分】
{checks_text}

【修改建议】
{suggestions_text}

【当前文章】
{article_text[:6000]}

【原文全文（参考，不要新增原文没有的事实）】
{original_text[:120000]}

请输出修改后的完整 Markdown 文章，以一级标题开头。
"""
    response = llm_provider.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=strategy.get("temperature", 0.5),
    )
    return polish_llm_article(response, article_type)


def save_article_states(
    report_path: Path,
    root: Path,
    article_states: list[dict],
    generated_count: int,
    github_count: int,
) -> None:
    """写入最终的文章状态文件（审稿循环完成后调用）。"""
    from src.common.utils import load_json, write_json

    data = load_json(report_path)
    states_dir = root / "states"
    states_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        states_dir / f"{data['date']}.article_states.json",
        {
            "date": data["date"],
            "source_report": str(report_path.relative_to(root)).replace("/", "\\"),
            "generation_summary": {
                "total_items": len(data.get("items", [])),
                "generated": generated_count,
                "github_count": github_count,
                "non_github_count": generated_count - github_count,
            },
            "harness_layers": {
                "context": "writer 直接从 report JSON 读取 items，内嵌类型推断和路径生成。",
                "tools": "抓取、评估、生成和验证由固定脚本执行。",
                "orchestration": "pipeline.py 串联日报、文章生成、审稿修订和验证阶段（已去掉 deepread 中间层）。",
                "memory_state": "单篇上下文保留到审稿 Agent 通过后才丢弃；不通过则带着审稿意见修改。",
                "evaluation_observation": "每篇生成前用 AI 评估原文质量，不好就跳过；生成后用独立审稿 Agent 做五维度评分。",
                "constraints_recovery": "失败阶段写入 errors/，长期规则需人工确认。",
            },
            "state_policy": "短期状态只记录当天 pipeline episode；跨天长期记忆只进入 AGENT.md。",
            "articles": article_states,
        },
    )
