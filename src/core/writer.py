"""src/core/writer.py - 文章写作模块。

职责：
- 规则写作器：基于 STYLE_CONTRACTS 和事实上下文生成 Markdown
- LLM 写作器：基于原文全文使用 LLM 生成并修订文章
- 文章后处理：润色、段落拆分、短篇扩展
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from src.common.utils import chinese_char_count, load_json, write_json
from src.core.deepread import STYLE_CONTRACTS
from src.core.context import (
    fact_or_fallback,
    fetch_original_context,
    source_subject,
)
from src.infrastructure.llm_client import OpenAICompatibleProvider


def build_intro(article_type: str, subject: str, claim: str, facts: list[str]) -> str:
    """根据文章类型构建导语段落。"""
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


def build_first_section(article_type: str, facts: list[str], claim: str) -> str:
    """构建文章第一节（事实层）。"""
    first = fact_or_fallback(facts, 0, "原文最重要的信息，是它给出了一个具体事件，而不是抽象口号。")
    second = fact_or_fallback(facts, 1, "这类信息需要先还原动作、对象和结果，再谈影响。")
    if article_type == "工具型":
        return (
            f"先从原文给出的对象说起。{first} {second} 所以它不应该被简单理解成又一个\"Agent 项目\"，"
            "而要看它有没有把任务、工具、状态和使用者放进同一个可运行的流程里。适合谁，也要从这里判断："
            "如果团队只是想做一次演示，包装就够了；如果要让 AI 反复参与真实任务，就必须关心安装、配置、权限和失败恢复。"
        )
    if article_type == "解读型":
        return (
            f"原文给出的第一层信息是具体进展。{first} {second} 这些事实本身未必足够宏大，"
            f"但它们共同指向一个判断：{claim} 技术解读的第一步不是喊趋势，而是说明这些进展为什么会影响接入方式、"
            "任务分工或者成本结构。"
        )
    return (
        f"先看事实层。{first} {second} 如果只把它当成一条热闹新闻，就会漏掉流程问题："
        "这次事件到底是一次展示，还是已经开始改变某个真实工作流。为什么重要，也要从这里回答："
        "只有当流程被拆开、责任被重新分配，AI 的能力才不只是一次结果展示。"
    )


def build_second_section(article_type: str, facts: list[str]) -> str:
    """构建文章第二节（细节层）。"""
    third = fact_or_fallback(facts, 2, "原文里的关键细节，通常藏在流程描述和能力边界里。")
    fourth = fact_or_fallback(facts, 3, "这些细节决定它是一次演示，还是可以继续被复用的方法。")
    if article_type == "工具型":
        return (
            f"{third} {fourth} 这也是工具文章最需要保留的部分：不是替项目写宣传语，"
            "而是把它放进一个具体任务里，看看安装、调用、权限、失败处理和审查有没有位置。"
            "比如一个工具如果宣称能服务 Agent，就要继续追问：它能不能限制 Agent 能访问的资源，"
            "能不能记录每一步操作，能不能在出错后让人知道该从哪里恢复。"
        )
    if article_type == "解读型":
        return (
            f"{third} {fourth} 趋势判断不能脱离这些细节。有效的解读，是说明为什么这些小变化会改变入口、"
            "成本、开发者习惯或组织流程。比如开发者过去只需要关心一次调用，现在可能要关心上下文怎么传递、"
            "工具怎么接入、失败怎么复盘，以及哪些步骤需要人类确认。"
        )
    return (
        f"{third} {fourth} 这说明主线型文章不能只复述标题，必须把事件拆成几步：谁推进了什么，"
        "交给 AI 的是什么，仍然由人判断的又是什么。比如研究、写作、代码或调研任务里，AI 可以承担执行链路，"
        "但选题、判断标准和最终取舍仍然需要明确责任。"
    )


def build_third_section(article_type: str, facts: list[str]) -> str:
    """构建文章第三节（影响层）。"""
    fifth = fact_or_fallback(facts, 4, "从使用者视角看，影响工作的是能不能把结果接进下一步。")
    sixth = fact_or_fallback(facts, 5, "如果缺少验证和复盘，再漂亮的结果也很难变成稳定流程。")
    if article_type == "工具型":
        return (
            f"{fifth} {sixth} 举个例子，一个 Agent 工具如果只能完成理想输入下的单次任务，价值会很有限；"
            "如果它能记录步骤、暴露失败、允许人工接管，才更接近团队愿意长期使用的基础设施。"
            "技术看点也在这里：不是模型多会说，而是工具是否让任务生命周期可见，是否能把一次失败变成下一轮规则。"
        )
    if article_type == "解读型":
        return (
            f"{fifth} {sixth} 对开发者来说，变化往往不是突然发生在模型分数上，而是发生在接入方式、"
            "上下文组织、调试成本和团队协作习惯上。这里可以把它理解成一种工作流变化：AI 不再只负责最后的生成，"
            "而是开始进入准备、执行、检查和复盘这些中间环节。"
        )
    return (
        f"{fifth} {sixth} 这也是这类事件的现实影响：它让人重新分配人与 AI 的职责，"
        "把原本靠个人经验推进的环节，变成可以检查、可以复用、也可以追责的流程。影响不会只落在某个模型上，"
        "而会落在团队如何设计任务、如何留下证据、如何决定哪些步骤必须人工确认。"
    )


def build_final_section(article_type: str, facts: list[str], contract: dict) -> str:
    """构建文章末节（观察层）。"""
    seventh = fact_or_fallback(facts, 6, "原文没有完全回答的问题，同样值得保留。")
    eighth = fact_or_fallback(facts, 7, "后续要看的不是一句口号，而是它能不能经受真实任务的反复检验。")
    if article_type == "工具型":
        return (
            f"{seventh} {eighth} 所以最后的判断要克制：少看它说自己能做什么，多看它在失败、权限、部署、"
            f"成本和人工审查面前怎么表现。{contract['tone']} 需要验证的不是一句介绍，而是它能否承受重复任务、"
            "异常输入和多人协作。"
        )
    if article_type == "解读型":
        return (
            f"{seventh} {eighth} 所以这篇文章的收束不是\"趋势已定\"，而是给出一个观察口径："
            f"以后看到类似动态，要看它是否真的降低了使用门槛，还是只换了一层包装。{contract['tone']} "
            "接下来更值得看的是真实用户会不会把它放进日常流程，而不是只在发布当天转发。"
        )
    return (
        f"{seventh} {eighth} 所以这件事的后续观察，不只是同类新闻还会不会出现，"
        "而是这套做法能不能在更多真实任务里保持稳定、透明和可复盘。"
    )


def expand_if_short(article: str, article_type: str, subject: str) -> str:
    """如果文章过短，补充编辑判断段落。"""
    if chinese_char_count(article) >= 1250:
        return article
    if article_type == "工具型":
        addition = f"""
把「{subject}」放到团队环境里看，判断标准会更清楚。一个要上线的工具，不能只在作者机器上跑通一次，还要能解释依赖什么环境、访问哪些资源、失败时留下什么线索。

Agent 工具一旦进入代码仓库、云资源或企业系统，权限、日志、回滚和审查都会变成硬问题。它是什么、适合谁、技术看点和局限，最后都要落到这些问题上。

换句话说，工具型文章的风格控制不靠固定模板，而靠问题清单：能不能运行，能不能接管，能不能审查，能不能在失败后继续。这些问题比一句漂亮介绍更接近真实使用。

如果后续继续观察这个项目，可以优先看三个变化：文档是否补齐真实任务示例，权限和状态是否能被清楚配置，失败日志是否能帮助下一次执行。只有这些细节变扎实，工具才有机会从候选仓库进入团队流程。

对读者来说，最实用的读法也很简单：先不要问它是不是很酷，而要问它能不能放进自己的工作流。如果答案还不清楚，就把它当成候选工具继续观察，而不是马上当成生产方案。
"""
    elif article_type == "解读型":
        addition = f"""
放到「{subject}」这个具体对象上，解读还需要多一层克制。它可以说明一个方向正在变热，但不能自动证明所有团队都会马上采用。分水岭在于开发者是否愿意把它接进已有流程。

所以后续应该看两个指标。第一，它有没有降低真实任务的接入成本；第二，它有没有让结果更容易检查和复盘。只有这两点成立，技术信号才会从一次热闹更新变成可持续的工作方式。

也可以换个角度看：如果这件事只带来一次新鲜感，它就是资讯；如果它改变了工具、上下文、权限或评测的组织方式，它才值得写成解读。

这也是解读型文章需要守住的边界。我们可以给出判断，但判断要能回到原文细节上；我们可以谈趋势，但趋势要能落到开发者下一步会怎么做、团队流程会怎么变。
"""
    else:
        addition = f"""
把「{subject}」放到具体团队流程里看，这次事件的意义会更清楚。它不是单纯告诉我们 AI 又能完成一个结果，而是在提醒团队重新划分任务：哪些步骤可以交给系统推进，哪些判断必须由人负责。

接下来应该看的不是一句口号，而是这套做法能不能反复运行。能反复运行，才说明它进入了工作流；只能偶尔惊艳一次，就仍然停留在案例层面。

主线型文章需要保持这个顺序：先让读者知道事实，再给判断；先还原动作，再谈影响。这样文章才像基于原文的改写，而不是换一个标题重新发挥。

所以这篇文章的收束点是可复用性：这次事件留下的是一次讨论热度，还是一种可以被更多人复用的方法。如果是后者，后续一定会出现更清楚的流程、边界和验证方式。
"""
    return article.rstrip() + "\n" + addition


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


def polish_llm_article(content: str, article_type: str) -> str:
    """对 LLM 生成的文章进行风格润色。"""
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
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    content = re.sub(
        r"真正([^。！？\n]{0,20})不是([^。！？\n]{0,60})而是",
        r"关键\1不在于\2，而在于",
        content,
    )

    heading_replacements = {
        "主线型": {
            "为什么重要": "影响开始落到分发和决策链路",
            "接下来怎么看": "后续要看闭环是否跑通",
            "后续观察": "后续要看闭环是否跑通",
        },
        "解读型": {
            "背后原因": "机制变化落在接入方式",
            "技术含义": "工程含义落在上下文和接口",
            "接下来": "落地还要看成本和复盘",
        },
        "工具型": {
            "它是什么": "定位先从任务入口说清楚",
            "适合谁": "适用人群取决于任务约束",
            "技术看点": "快照、状态和门禁决定可用性",
            "局限": "边界要放到失败场景里验证",
            "需要验证": "边界要放到失败场景里验证",
        },
    }
    mapping = heading_replacements.get(article_type, {})
    lines = []
    for line in content.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            for marker, replacement in mapping.items():
                if heading.startswith(marker):
                    heading = replacement
                    break
            line = f"## {heading}"
        lines.append(line)
    return wrap_long_paragraphs("\n".join(lines))


def ensure_full_original_for_llm(context: dict, item: dict, max_chars: int) -> str:
    """确保 LLM 写作有完整的原文全文。"""
    original_text = str(context.get("original_text") or "")
    status = context.get("status")
    if status != "fetched" or len(original_text) < 300:
        raise RuntimeError(
            "LLM writer requires fetched full original text for each article; "
            f"got status={status!r} for {item.get('title', '')!r}."
        )
    if len(original_text) > max_chars:
        raise RuntimeError(
            "Original text exceeds --llm-max-original-chars; refusing to excerpt automatically. "
            f"chars={len(original_text)}, limit={max_chars}, title={item.get('title', '')!r}."
        )
    return original_text


def build_llm_writer_prompt(item: dict, context: dict, original_text: str) -> list[dict]:
    """构建 LLM 写作的 prompt。"""
    plan = item.get("article_plan") or {}
    article_type = item.get("article_type", "")
    contract = STYLE_CONTRACTS.get(article_type, {})
    must_include = plan.get("must_include") or []
    github_note = ""
    if article_type == "工具型" and "github.com/" in str(item.get("url", "")).lower():
        github_note = f"- 这是 GitHub/开源项目文章，标题后 8 行内必须保留项目链接：{item.get('url')}\n"
    type_checklist = {
        "主线型": (
            "- 主线型必须自然覆盖：事实切入、为什么重要、影响或改变、后续观察。\n"
            "- 正文里要出现\"这次事件\"或\"这次进展\"，并明确写出\"影响\"与\"接下来/后续\"。\n"
        ),
        "解读型": (
            "- 解读型必须自然覆盖：信号边界、机制解释、技术含义、影响判断。\n"
            "- 正文里要出现\"信号/边界/观察\"之一，\"接入方式/机制/流程/成本结构\"之一，以及\"技术/上下文/API/SDK/MCP/harness\"之一。\n"
        ),
        "工具型": (
            "- 工具型必须自然覆盖：工具定义、适合人群、技术看点、局限提醒。\n"
            "- 正文里要出现\"技术看点\"，也要出现\"局限/风险/需要验证/谨慎\"之一，但不要把二级标题直接写成\"技术看点\"。\n"
        ),
    }.get(article_type, "")
    system = (
        "你是严谨的技术公众号编辑。你只能基于用户提供的原文全文写作，"
        "不得联网，不得添加原文没有的事实，不得把标题、关键词或摘要当作事实来源。"
    )
    user = f"""请基于下面的\"原文全文\"写一篇原创中文 Markdown 文章。

硬性规则：
- 只能使用原文全文中的事实；允许重组、解释、压缩和改写，但不允许新增事实。
- 如果原文信息不足，必须降低判断强度，不要补细节。
- 当前文章写完后，原文上下文会被丢弃；不要引用上一篇或其他文章的信息。
- Markdown 必须以一个一级标题开头，且只能有一个一级标题。
- 至少 3 个二级标题，二级标题必须写成具体判断。
- 中文正文 1300-1800 字，不能短于 1200 字。
- 段落以 2-4 句话为主，避免大量单句短段落。
- 至少写一个具体例子或类比，使用\"比如\"或\"举个例子\"自然展开。
- 不写\"参考来源\"\"来源\"\"据某媒体报道\"等归因区块。
- 不写\"真正值得看\"\"背后的技术信号\"\"这条动态值得拆开看\"\"不只要看功能\"等模板腔。
{github_note}
{type_checklist}
文章类型：{article_type}
类型写法：{contract.get("position", "")}
必须回答：{"；".join(contract.get("must_answer", []))}
标题建议：{plan.get("title", "")}
核心判断：{plan.get("core_claim", "")}
必须自然覆盖的关键对象：{"；".join(map(str, must_include))}

原文标题：{item.get("title", "")}
原文 URL：{item.get("url", "")}

原文全文：
{original_text}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_llm_revision_prompt(
    item: dict,
    original_text: str,
    current_text: str,
    verification: dict,
) -> list[dict]:
    """构建 LLM 修订的 prompt。"""
    system = (
        "你是严谨的技术公众号编辑。你正在修订一篇未通过校验的文章。"
        "只能基于原文全文和当前文章修订，不得新增原文没有的事实。"
    )
    errors = "\n".join(f"- {msg}" for msg in verification.get("errors", [])) or "- 无"
    warnings = "\n".join(f"- {msg}" for msg in verification.get("warnings", [])) or "- 无"
    suggestions = "\n".join(f"- {msg}" for msg in verification.get("rewrite_suggestions", [])) or "- 无"
    article_type = item.get("article_type", "")
    type_checklist = {
        "主线型": "自然补齐：事实切入、为什么重要、影响或改变、后续观察；正文需要有\"这次事件/这次进展\"\"影响\"\"接下来/后续\"。",
        "解读型": "自然补齐：信号边界、机制解释、技术含义、影响判断；正文需要有\"信号/边界/观察\"\"接入方式/机制/流程/成本结构\"\"技术/上下文/API/SDK/MCP/harness\"。",
        "工具型": "自然补齐：工具定义、适合人群、技术看点、局限提醒；正文需要有\"技术看点\"和\"局限/风险/需要验证/谨慎\"。",
    }.get(article_type, "")
    user = f"""请修订下面的 Markdown 文章，让它通过校验。

硬性规则：
- 只能基于原文全文修订，不允许新增事实。
- 保留一个一级标题，至少 3 个二级标题。
- 中文正文 1300-1800 字，不能短于 1200 字。
- 段落以 2-4 句话为主，避免大量单句短段落。
- 至少写一个具体例子或类比，使用\"比如\"或\"举个例子\"自然展开。
- {type_checklist}
- 不写\"真正值得看\"\"背后的技术信号\"\"这条动态值得拆开看\"\"不只要看功能\"等模板腔。
- 直接输出完整 Markdown，不要解释。

文章类型：{article_type}
原文标题：{item.get("title", "")}
原文 URL：{item.get("url", "")}

校验错误：
{errors}

校验警告：
{warnings}

重写建议：
{suggestions}

当前文章：
{current_text}

原文全文：
{original_text}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def rewrite_one_article_with_llm(
    item: dict,
    context: dict,
    llm_provider: OpenAICompatibleProvider,
    llm_max_original_chars: int,
) -> tuple[str, dict]:
    """使用 LLM 重写单篇文章。"""
    original_text = ensure_full_original_for_llm(context, item, llm_max_original_chars)
    article = llm_provider.chat(
        build_llm_writer_prompt(item, context, original_text),
    )
    article = polish_llm_article(article, str(item.get("article_type", "")))
    rewrite_state = {
        "writer": "llm",
        "source_status": context.get("status"),
        "fact_count": len(context.get("facts", [])),
        "key_terms": context.get("terms", []),
        "original_char_count": context.get("original_char_count", 0),
        "context_discarded": True,
        "llm_model": llm_provider.model,
    }
    return article, rewrite_state


def rewrite_one_article(
    item: dict,
    use_llm_writer: bool = False,
    llm_provider: OpenAICompatibleProvider | None = None,
    llm_max_original_chars: int = 120000,
) -> tuple[str, dict]:
    """重写单篇文章（规则或 LLM 模式）。

    返回 (文章文本, 状态信息)。
    调用方负责：写完即丢弃 context 中的 original_text。
    """
    article_type = item["article_type"]
    contract = STYLE_CONTRACTS[article_type]
    plan = item["article_plan"]
    context = fetch_original_context(item)

    if use_llm_writer:
        if llm_provider is None:
            raise RuntimeError("use_llm_writer=True 需要提供 llm_provider")
        article, rewrite_state = rewrite_one_article_with_llm(
            item=item,
            context=context,
            llm_provider=llm_provider,
            llm_max_original_chars=llm_max_original_chars,
        )
        context.pop("original_text", None)
        del context
        return article, rewrite_state

    # 规则写作器
    facts = context["facts"]
    title = str(plan["title"])
    subject = context["subject"]
    claim = str(plan.get("core_claim", ""))
    project_line = ""
    if article_type == "工具型" and "github.com/" in str(item.get("url", "")).lower():
        project_line = f"\n项目链接：{item['url']}\n"

    sections = contract["sections"]
    intro = build_intro(article_type, subject, claim, facts)
    body = [
        f"# {title}",
        "",
        intro + project_line,
        "",
        f"## {sections[0]}",
        "",
        build_first_section(article_type, facts, claim),
        "",
        f"## {sections[1]}",
        "",
        build_second_section(article_type, facts),
        "",
        f"## {sections[2]}",
        "",
        build_third_section(article_type, facts),
        "",
        f"## {sections[3]}",
        "",
        build_final_section(article_type, facts, contract),
        "",
    ]
    article = "\n".join(body)
    article = expand_if_short(article, article_type, subject)
    article = wrap_long_paragraphs(article)
    if chinese_char_count(article) < 1200:
        article = wrap_long_paragraphs(
            article.rstrip()
            + "\n\n"
            + f"补充一层编辑判断：围绕「{subject}」写作时，质量来自事实、判断和边界的配合。"
            + "事实负责把对象说准，判断负责指出变化方向，边界负责避免把单条材料拔高成结论。"
            + "如果原文能提供足够细节，文章就多还原动作和流程；如果原文细节有限，文章就明确观察口径，用谨慎语气处理不确定部分。"
            + "当天短期状态也因此有必要保留：它能标出某篇文章是原文事实充足，还是主要依赖摘要和标题完成改写。"
            + "两种情况都可以进入编辑流程，但发布判断应该不同。"
            + "\n"
        )
    rewrite_state = {
        "writer": "rule_based",
        "source_status": context.get("status"),
        "fact_count": len(facts),
        "key_terms": context.get("terms", []),
        "original_char_count": context.get("original_char_count", 0),
        "context_discarded": True,
    }
    context.pop("original_text", None)
    del context
    return article, rewrite_state


def verify_article_json(output: Path, deepread_path: Path, root: Path) -> dict:
    """通过 verify_article.py 验证单篇文章，返回 JSON 结果。"""
    completed = subprocess.run(
        [
            sys.executable,
            "src/validators/verify_article.py",
            str(output.relative_to(root)).replace("/", "\\"),
            "--deepread",
            str(deepread_path.relative_to(root)).replace("/", "\\"),
            "--json",
        ],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Could not parse verify_article.py JSON output: {completed.stdout}"
        ) from exc
    if not payload:
        raise RuntimeError("verify_article.py returned an empty result list.")
    return payload[0]


def revise_article_with_llm_until_valid(
    item: dict,
    output: Path,
    deepread_path: Path,
    root: Path,
    current_text: str,
    rewrite_state: dict,
    llm_provider: OpenAICompatibleProvider,
    max_attempts: int,
    llm_max_original_chars: int,
) -> tuple[str, dict]:
    """反复修订文章直到通过验证或耗尽尝试次数。"""
    attempts = 0
    for attempts in range(1, max_attempts + 1):
        verification = verify_article_json(output, deepread_path, root)
        if verification.get("passed"):
            rewrite_state["llm_revision_attempts"] = attempts - 1
            return current_text, rewrite_state

        context = fetch_original_context(item)
        original_text = ensure_full_original_for_llm(context, item, llm_max_original_chars)
        revised = llm_provider.chat(
            build_llm_revision_prompt(item, original_text, current_text, verification),
            temperature=0.35,
        )
        revised = polish_llm_article(revised, str(item.get("article_type", "")))
        output.write_text(revised, encoding="utf-8")
        current_text = revised
        rewrite_state["original_char_count"] = context.get("original_char_count", 0)
        context.pop("original_text", None)
        del context

    final_verification = verify_article_json(output, deepread_path, root)
    rewrite_state["llm_revision_attempts"] = attempts
    rewrite_state["llm_revision_passed"] = bool(final_verification.get("passed"))
    return current_text, rewrite_state


def generate_articles(
    deepread_path: Path,
    root: Path,
    overwrite: bool = False,
    use_llm_writer: bool = False,
    llm_provider: OpenAICompatibleProvider | None = None,
    llm_rewrite_attempts: int = 0,
    llm_max_original_chars: int = 120000,
) -> None:
    """从 deepread 生成所有文章。

    Args:
        deepread_path: deepread JSON 文件路径
        root: 项目根目录
        overwrite: 是否覆盖已有文章
        use_llm_writer: 是否使用 LLM 写作器
        llm_provider: LLM provider 实例
        llm_rewrite_attempts: LLM 修订最大尝试次数
        llm_max_original_chars: 原文最大字符数限制
    """
    data = load_json(deepread_path)
    paper_dir = root / "daily_paper"
    states_dir = root / "states"
    paper_dir.mkdir(parents=True, exist_ok=True)
    states_dir.mkdir(parents=True, exist_ok=True)
    article_states: list[dict] = []
    for item in data.get("selected_items", []):
        output = root / item["output_file"]
        base_state = {
            "date": data.get("date"),
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "article_type": item.get("article_type", ""),
            "output_file": item.get("output_file", ""),
            "context_scope": "single_article",
            "memory_policy": "discard_after_write",
            "stage": "pending",
        }
        if output.exists() and not overwrite:
            print(f"Keep existing {output.relative_to(root)}")
            article_states.append(
                {
                    **base_state,
                    "stage": "kept_existing",
                    "output_exists": True,
                }
            )
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        text, rewrite_state = rewrite_one_article(
            item,
            use_llm_writer=use_llm_writer,
            llm_provider=llm_provider,
            llm_max_original_chars=llm_max_original_chars,
        )
        output.write_text(text, encoding="utf-8")
        if use_llm_writer and llm_rewrite_attempts > 0 and llm_provider:
            text, rewrite_state = revise_article_with_llm_until_valid(
                item=item,
                output=output,
                deepread_path=deepread_path,
                root=root,
                current_text=text,
                rewrite_state=rewrite_state,
                llm_provider=llm_provider,
                max_attempts=llm_rewrite_attempts,
                llm_max_original_chars=llm_max_original_chars,
            )
        print(f"Generated {output.relative_to(root)}")
        article_states.append(
            {
                **base_state,
                **rewrite_state,
                "stage": "drafted",
                "output_exists": output.exists(),
                "char_count": chinese_char_count(text),
            }
        )
        del text
    write_json(
        states_dir / f"{data['date']}.article_states.json",
        {
            "date": data["date"],
            "source_deepread": str(deepread_path.relative_to(root)).replace("/", "\\"),
            "harness_layers": {
                "context": "deepread 只保存当前文章需要的选择理由、写作计划和关键对象。",
                "tools": "抓取、生成和验证由固定脚本执行。",
                "orchestration": "pipeline.py 串联日报、deepread、文章和验证阶段。",
                "memory_state": "当天状态写入本文件；单篇上下文写完即丢弃。",
                "evaluation_observation": "验证结果回写到对应文章状态。",
                "constraints_recovery": "失败阶段写入 errors/，长期规则需人工确认。",
            },
            "state_policy": "短期状态只记录当天 pipeline episode；跨天长期记忆只进入 AGENT.md。",
            "articles": article_states,
        },
    )
