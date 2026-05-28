"""src/core/context.py - 原文抓取、事实提取与上下文管理模块。

职责：
- 从原文 URL 抓取全文并提取可用事实句子
- 句子评分、去重、清洗
- 管理单篇文章的原文上下文生命周期（用完即丢弃）
"""

from __future__ import annotations

import re
from urllib.error import HTTPError, URLError

from src.common.utils import chinese_char_count, clean_text, fetch_text


def extract_required_terms(text: str, url: str = "") -> list[str]:
    """从文本中提取关键术语，用于 must_include 约束。"""
    terms: list[str] = []
    ignored = {
        "https", "http", "www", "com", "for", "true", "skip",
        "data-turbo-transient", "to",
    }
    for match in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", text):
        if match.lower() in ignored or "..." in match:
            continue
        terms.append(match)
    for match in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_.-]{3,}", text):
        if re.search(r"[\u4e00-\u9fff]", match) and len(match) <= 18:
            terms.append(match)
    if "github.com/" in url.lower():
        repo = url.rstrip("/").split("github.com/")[-1]
        terms.extend([part for part in repo.split("/") if part])

    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        normalized = term.strip("，。！？、：:；;（）()[]【】「」\"'")
        if len(normalized) < 3 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= 8:
            break
    return result


def split_sentences(text: str) -> list[str]:
    """将文本拆分为可用句子（过滤过短和过长）。"""
    pieces = re.split(r"(?<=[。！？.!?])\s*", text)
    sentences = []
    for piece in pieces:
        sentence = piece.strip()
        if 24 <= chinese_char_count(sentence) <= 180:
            sentences.append(sentence)
    return sentences


def title_terms(title: str) -> list[str]:
    """从标题提取关键术语。"""
    return extract_required_terms(title)


def score_sentence(sentence: str, terms: list[str], article_type: str) -> int:
    """对句子打分：术语匹配 + 类型相关关键词。"""
    score = sum(3 for term in terms if term and term in sentence)
    score += len(re.findall(
        r"\d+|AI|Agent|MCP|SDK|API|模型|工具|论文|项目|研究|代码|开发",
        sentence,
    ))
    if article_type == "工具型":
        score += len(re.findall(r"安装|使用|仓库|开源|项目|工具|部署|配置|运行", sentence))
    elif article_type == "解读型":
        score += len(re.findall(r"趋势|原因|意味着|背后|技术|行业|开发者", sentence))
    else:
        score += len(re.findall(r"发布|宣布|显示|开发|完成|提出|表示|事件|进展", sentence))
    return score


def is_usable_fact_sentence(sentence: str) -> bool:
    """判断句子是否是可用的事实句子（排除噪声）。"""
    noise_patterns = [
        "来源：", "量子位", "机器之心", "扫码", "相关阅读",
        "参考链接", "热门文章", "版权所有", "ICP备",
        "关于我们", "加入我们", "商务合作", "首页",
    ]
    if any(pattern in sentence for pattern in noise_patterns):
        return False
    if len(re.findall(r"\d{4}-\d{2}-\d{2}", sentence)) >= 2:
        return False
    return True


def sanitize_fact_sentence(sentence: str) -> str:
    """清洗事实句子：移除来源标注、合并空白。"""
    sentence = re.sub(r"来源[:：].*$", "", sentence)
    sentence = re.sub(r"\s+", " ", sentence)
    return sentence.strip()


def dedupe_sentences(sentences: list[str]) -> list[str]:
    """按指纹去重句子。"""
    result: list[str] = []
    fingerprints: set[str] = set()
    for sentence in sentences:
        fingerprint = re.sub(r"\W+", "", sentence.lower())[:40]
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        result.append(sentence)
    return result


def soften_fact(sentence: str) -> str:
    """软化事实句子：去除时间前缀，补充句号。"""
    sentence = re.sub(r"\s+", " ", sentence).strip()
    sentence = re.sub(r"^(近日|日前|今天|目前)[，,]?", "", sentence)
    sentence = sentence.strip(" 。")
    if not sentence:
        return ""
    return sentence + "。"


def fact_or_fallback(facts: list[str], index: int, fallback: str) -> str:
    """从事实列表中取第 index 条，不足时返回 fallback。"""
    if index < len(facts):
        return soften_fact(facts[index])
    return fallback


def source_subject(item: dict) -> str:
    """从条目中提取文章主题。"""
    title = str(item.get("title", "这条 AI 动态"))
    title = re.sub(r"GitHub 项目更新：", "", title)
    return title.strip(" .")[:42] or "这条 AI 动态"


def fetch_original_context(item: dict) -> dict:
    """抓取原文并提取事实上下文。

    返回 dict 包含 status, subject, terms, facts, original_text 等字段。
    原文全文仅在 local context 中存在，调用方用完必须丢弃。

    微信文章优先使用 Selenium 浏览器抓取（绕过反爬）。
    """
    url = str(item.get("url", ""))
    title = str(item.get("title", ""))
    article_type = item.get("article_type", "主线型")
    fallback = "。".join(
        part
        for part in [
            title,
            str(item.get("selection_reason", "")),
            str(item.get("article_plan", {}).get("core_claim", "")),
            str(item.get("article_plan", {}).get("original_summary", "")),
            str(item.get("article_plan", {}).get("original_insight", "")),
        ]
        if part
    )
    raw_text = ""
    fetch_status = "failed"
    if url:
        # 微信文章 / 搜狗跳转链接：优先使用 Selenium 浏览器抓取
        is_wechat = "mp.weixin.qq.com" in url
        is_sogou_redirect = "weixin.sogou.com" in url and "/link?" in url
        if is_wechat or is_sogou_redirect:
            try:
                from src.infrastructure.browser_fetcher import (
                    create_browser, fetch_wechat_article, resolve_wechat_url,
                )
                with create_browser(headless=True) as driver:
                    target_url = url
                    if is_sogou_redirect:
                        resolved = resolve_wechat_url(driver, url)
                        if resolved:
                            target_url = resolved
                        else:
                            raise RuntimeError("无法解析搜狗跳转链接")
                    browser_text = fetch_wechat_article(driver, target_url)
                    if browser_text and len(browser_text) >= 300:
                        raw_text = browser_text
                        fetch_status = "fetched"
            except Exception:
                pass

        # urllib 回退（或非微信文章）
        if fetch_status == "failed":
            try:
                raw_text = clean_text(fetch_text(url))
                fetch_status = "fetched" if len(raw_text) >= 300 else "partial"
            except (HTTPError, URLError, TimeoutError, ValueError, OSError):
                raw_text = fallback
                fetch_status = "failed"

    text = raw_text if raw_text else fallback
    terms = title_terms(title)
    sentences = split_sentences(text)
    usable_sentences = [
        sanitize_fact_sentence(sentence)
        for sentence in sentences
        if is_usable_fact_sentence(sentence)
    ]
    ranked = sorted(
        usable_sentences,
        key=lambda sentence: score_sentence(sentence, terms, article_type),
        reverse=True,
    )
    facts = dedupe_sentences(ranked[:10])
    if len(facts) < 4:
        facts.extend(
            sentence for sentence in split_sentences(fallback) if sentence not in facts
        )
    if "github.com/" in url.lower() and len(facts) < 4:
        repo = url.rstrip("/").split("github.com/")[-1]
        facts.extend(
            [
                f"{repo} 是一个近期更新的 GitHub 项目，标题和仓库描述把它放在 AI Coding 语境下讨论。",
                "工具型项目要检查运行边界、失败处理、权限控制和日志线索。",
                "如果一个项目要进入团队工作流，部署、权限、日志和人工审查都需要提前设计。",
            ]
        )
    return {
        "status": fetch_status,
        "subject": source_subject(item),
        "terms": terms,
        "facts": facts[:8],
        "original_text": raw_text if fetch_status in {"fetched", "partial"} else "",
        "original_char_count": chinese_char_count(raw_text),
    }
