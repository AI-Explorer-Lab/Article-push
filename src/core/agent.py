"""src/core/agent.py - Live AI technology daily report generator.

Pipeline:
1. Fetch lead articles from the user's requested sources:
   - Challenge Hub, 量子位, AI学习的老章 through Sogou WeChat search.
   - OpenAI, Anthropic, Google DeepMind, Jiqizhixin, GitHub through public pages/APIs.
2. Fetch article pages again as second-stage evidence when possible.
3. Rank, classify, summarize, and write reports/YYYY-MM-DD.json for verify.py.

The script uses only Python's standard library so the harness can run without
installing dependencies.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse

from src.common.utils import USER_AGENT, clean_text, fetch_text, strip_tags

# ---------------------------------------------------------------------------
# 关注领域及其关键词映射（需与 AGENT.md 保持一致）
# ---------------------------------------------------------------------------
FOCUS_TOPICS: dict[str, list[str]] = {
    "AI Agent": [
        "agent",
        "agentic",
        "智能体",
        "多智能体",
        "autonomous",
        "tool use",
        "工具调用",
        "workflow",
    ],
    "Harness Engineering": [
        "harness",
        "workflow",
        "orchestration",
        "评测",
        "benchmark",
        "pipeline",
        "系统工程",
        "可靠性",
    ],
    "Context Engineering": [
        "context",
        "上下文",
        "memory",
        "rag",
        "检索增强",
        "知识库",
    ],
    "MCP": ["mcp", "model context protocol", "server tooling", "connector"],
    "AI Coding": [
        "coding",
        "code",
        "codex",
        "claude code",
        "copilot",
        "github actions",
        "代码",
        "编程",
    ],
    "Vibe Coding": ["vibe coding", "氛围编程"],
    "LLM推理与优化": [
        "推理",
        "reasoning",
        "优化",
        "inference",
        "量化",
        "distillation",
        "蒸馏",
        "token",
        "latency",
        "延迟",
    ],
    "多模态大模型": [
        "多模态",
        "multimodal",
        "视觉",
        "vision",
        "语音",
        "audio",
        "视频",
        "video",
        "图像生成",
        "image generation",
    ],
}

LEAD_SOURCES = [
    "Challenge Hub（微信公众号，经搜狗微信搜索）",
    "量子位（微信公众号，经搜狗微信搜索）",
    "AI学习的老章（微信公众号，经搜狗微信搜索）",
    "机器之心",
    "OpenAI Blog",
    "Anthropic News",
    "Google DeepMind Blog",
    "GitHub Trending/Search",
]

WECHAT_ONLY_TODAY = True

INSIGHTS = {
    "AI Agent": "值得关注它是否把规划、工具调用、反馈评估做成闭环，而不是停留在聊天式能力展示。",
    "Harness Engineering": "这类进展说明 AI 落地正在进入工程约束阶段，稳定性、成本和可观测性会决定真实价值。",
    "Context Engineering": "上下文组织正在成为 Agent 质量上限，检索、记忆和权限边界需要作为同一套系统设计。",
    "MCP": "MCP 相关动态会影响 Agent 能连接多少真实系统，是从演示走向生产的关键基础设施。",
    "AI Coding": "Coding Agent 进入真实仓库后，权限、测试、审查和供应链安全需要和生成能力同步建设。",
    "Vibe Coding": "Vibe Coding 的价值不只在生成速度，更在能否把需求、实现、验证串成可追踪流程。",
    "LLM推理与优化": "推理优化正在从实验室走向工程实践，模型能力上限和实际可用性之间的鸿沟在缩小。",
    "多模态大模型": "多模态能力扩展了 AI 的感知边界，但真正的挑战在于不同模态之间的对齐和可靠性。",
}


@dataclass
class Candidate:
    title: str
    source: str
    url: str
    published_at: str = ""
    snippet: str = ""
    body: str = ""
    evidence: list[dict] = field(default_factory=list)


@dataclass
class FetchLog:
    source: str
    url: str
    ok: bool
    detail: str


def parse_date(value: str) -> str | None:
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def parse_sogou_date(block: str) -> str | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", block)
    if match:
        return match.group(1)
    return None


def within_days(value: str, days: int) -> bool:
    parsed = parse_date(value)
    if not parsed:
        return False
    dt = datetime.strptime(parsed, "%Y-%m-%d")
    now = datetime.now()
    delta = now - dt
    return timedelta(0) <= delta <= timedelta(days=days)


def fetch_sogou_wechat(account: str, query_terms: list[str], logs: list[FetchLog]) -> list[Candidate]:
    candidates: list[Candidate] = []
    query = " ".join(query_terms[:3])
    search_url = f"https://weixin.sogou.com/weixin?type=2&s_from=input&query={quote(query)}"
    try:
        html_text = fetch_text(search_url)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        logs.append(FetchLog(source=account, url=search_url, ok=False, detail=str(exc)))
        return candidates
    blocks = re.split(r"<li\b", html_text)[1:]
    for block in blocks[:8]:
        title_match = re.search(r"<!--red_beg-->(.*?)<!--red_end-->", block, flags=re.S)
        url_match = re.search(r'href="(https?://mp\.weixin\.qq\.com[^"]+)"', block)
        if not title_match or not url_match:
            continue
        title = clean_text(title_match.group(1))
        url = url_match.group(1)
        pub_date = parse_sogou_date(block)
        if WECHAT_ONLY_TODAY and pub_date != date.today().isoformat():
            continue
        candidates.append(
            Candidate(
                title=title,
                source=account,
                url=url,
                published_at=pub_date or "",
                snippet=clean_text(block),
            )
        )
        logs.append(FetchLog(source=account, url=url, ok=True, detail=f"title={title[:40]}"))
    return candidates


def fetch_github(days: int, logs: list[FetchLog]) -> list[Candidate]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    query = quote(f"AI agent context MCP created:>{since}")
    url = f"https://api.github.com/search/repositories?q={query}&sort=stars&order=desc&per_page=10"
    candidates: list[Candidate] = []
    try:
        response = fetch_text(url)
        data = json.loads(response)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logs.append(FetchLog(source="GitHub", url=url, ok=False, detail=str(exc)))
        return candidates
    for repo in data.get("items", [])[:10]:
        candidates.append(
            Candidate(
                title=repo.get("full_name", ""),
                source="GitHub Trending/Search",
                url=repo.get("html_url", ""),
                published_at=repo.get("created_at", "")[:10],
                snippet=repo.get("description", ""),
            )
        )
    logs.append(FetchLog(source="GitHub", url=url, ok=True, detail=f"found {len(candidates)} repos"))
    return candidates


def extract_links_from_page(
    source: str, page_url: str, include_pattern: str, days: int,
    logs: list[FetchLog], limit: int = 15,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    try:
        html_text = fetch_text(page_url)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        logs.append(FetchLog(source=source, url=page_url, ok=False, detail=str(exc)))
        return candidates
    links = re.findall(
        rf'href="(https?://[^"]*{include_pattern}[^"]*)"',
        html_text,
        flags=re.I,
    )
    seen: set[str] = set()
    for link in links[:limit]:
        if link in seen:
            continue
        seen.add(link)
        text_surrounding = html_text.split(link)[-1][:200]
        title = clean_text(text_surrounding)[:100]
        candidates.append(
            Candidate(
                title=title or link.split("/")[-1],
                source=source,
                url=link,
            )
        )
    logs.append(FetchLog(source=source, url=page_url, ok=True, detail=f"extracted {len(candidates)} links"))
    return candidates


def enrich_candidate(candidate: Candidate) -> None:
    if candidate.body:
        return
    try:
        candidate.body = clean_text(fetch_text(candidate.url))
    except (HTTPError, URLError, TimeoutError, ValueError):
        candidate.body = ""


def classify(candidate: Candidate) -> str:
    text = f"{candidate.title} {candidate.snippet} {candidate.body}".lower()
    for topic, keywords in FOCUS_TOPICS.items():
        if any(keyword in text for keyword in keywords):
            return topic
    return "AI Agent"


def make_summary(candidate: Candidate, category: str) -> str:
    body = candidate.body or candidate.snippet
    sentences = re.split(r"(?<=[。！？.!?])\s*", clean_text(body))
    parts = [s.strip() for s in sentences if 15 < len(s.strip()) < 120]
    return (parts[0] if parts else candidate.title)[:100]


def normalize_title(title: str) -> str:
    title = re.sub(r"【[^】]*】", "", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip()[:80]


def to_report_item(candidate: Candidate) -> dict:
    category = classify(candidate)
    insight = INSIGHTS.get(category, INSIGHTS["AI Agent"])
    relevance = min(5, 2 + len(candidate.body) // 400)
    return {
        "title": normalize_title(candidate.title),
        "source": candidate.source,
        "url": candidate.url,
        "date": candidate.published_at or date.today().isoformat(),
        "category": category,
        "summary": make_summary(candidate, category),
        "insight": insight[:150],
        "relevance": relevance,
    }


def collect_candidates(days: int, logs: list[FetchLog]) -> list[Candidate]:
    all_candidates: list[Candidate] = []

    # 微信公众号来源
    wechat_sources = [
        ("Challenge Hub（微信公众号，经搜狗微信搜索）", ["AI", "Agent"]),
        ("量子位（微信公众号，经搜狗微信搜索）", ["AI", "Agent"]),
        ("AI学习的老章（微信公众号，经搜狗微信搜索）", ["AI", "Agent"]),
    ]
    for name, terms in wechat_sources:
        all_candidates.extend(fetch_sogou_wechat(name, terms, logs))

    # GitHub
    all_candidates.extend(fetch_github(days, logs))

    # 官方博客
    blog_sources = [
        ("OpenAI Blog", "https://openai.com/blog/", "openai"),
        ("Anthropic News", "https://www.anthropic.com/research", "anthropic"),
        ("Google DeepMind Blog", "https://deepmind.google/discover/blog/", "deepmind"),
        ("机器之心", "https://www.jiqizhixin.com/articles", "jiqizhixin"),
    ]
    for source, url, pattern in blog_sources:
        all_candidates.extend(
            extract_links_from_page(source, url, pattern, days, logs)
        )

    return all_candidates


def source_bucket(source: str) -> str:
    if "微信公众号" in source:
        return "微信公众号线索"
    if "GitHub" in source:
        return "GitHub"
    if any(name in source for name in ["OpenAI", "Anthropic", "DeepMind"]):
        return "Blog"
    if "机器之心" in source:
        return "媒体"
    return "社区"


def dedupe_report_items(items: list[dict], limit: int) -> list[dict]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    source_counts: dict[str, int] = {}
    result: list[dict] = []

    # 第一轮：去重 + 来源上限
    for item in items:
        url = item.get("url", "")
        title = item.get("title", "")
        bucket = source_bucket(item.get("source", ""))
        if url in seen_urls:
            continue
        if title in seen_titles:
            continue
        if source_counts.get(bucket, 0) >= 3:
            continue
        seen_urls.add(url)
        seen_titles.add(title)
        source_counts[bucket] = source_counts.get(bucket, 0) + 1
        result.append(item)

    # 第二轮：如果不足 limit，放宽来源限制
    if len(result) < limit:
        for item in items:
            if len(result) >= limit:
                break
            if item in result:
                continue
            url = item.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            result.append(item)

    return result[:limit]


def build_report(report_date: str, days: int, limit: int) -> dict:
    logs: list[FetchLog] = []
    candidates = collect_candidates(days, logs)
    for candidate in candidates:
        enrich_candidate(candidate)
    items = [to_report_item(c) for c in candidates]
    items = dedupe_report_items(items, limit)
    # 按相关度降序排列
    items.sort(key=lambda row: (row.get("relevance", 0), row.get("date", "")), reverse=True)
    return {
        "date": report_date,
        "topic_focus": "前沿 AI 技术动态，聚焦 AI Agent、Harness Engineering、Context Engineering 与多模态大模型。",
        "items": items,
        "_fetch_logs": [asdict(log) for log in logs],
    }


def default_report_path(report_date: str) -> Path:
    return Path("reports") / f"{report_date}.json"


def write_report(output_path: Path, report_date: str, days: int, limit: int) -> Path:
    report = build_report(report_date, days, limit)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Report written: {output_path} ({len(report['items'])} items)")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the AI tech daily report.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Report date in YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=10, help="Lookback window in days.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum report items to write.")
    parser.add_argument("--output", default=None, help="Custom output path; defaults to reports/YYYY-MM-DD.json.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = Path(args.output) if args.output else default_report_path(args.date)
    write_report(output_path, args.date, args.days, args.limit)


if __name__ == "__main__":
    main()
