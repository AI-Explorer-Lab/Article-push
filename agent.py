"""Live AI technology daily report generator.

Pipeline:
1. Fetch lead articles from the user's requested sources:
   - Challenge Hub, QbitAI, AI学习的老章 through Sogou WeChat search.
   - QbitAI through its public WordPress JSON mirror.
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
from urllib.request import Request, urlopen


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

FOCUS_TOPICS = {
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
}

LEAD_SOURCES = [
    "Challenge Hub（微信公众号，经搜狗微信搜索）",
    "量子位（微信公众号 / QbitAI 网页镜像）",
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


def fetch_text(url: str, timeout: int = 18) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def strip_tags(value: str) -> str:
    return clean_text(value)


def parse_date(value: str) -> str:
    if not value:
        return date.today().isoformat()
    value = value.strip()
    for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[: len(pattern)], pattern).date().isoformat()
        except ValueError:
            pass
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return date.today().isoformat()


def parse_sogou_date(block: str) -> str:
    timestamp_match = re.search(r"timeConvert\('(\d+)'\)", block)
    if timestamp_match:
        return datetime.fromtimestamp(int(timestamp_match.group(1))).date().isoformat()

    date_match = re.search(r'<span class="s2"[^>]*>(.*?)</span>', block, flags=re.S | re.I)
    if date_match:
        return parse_date(strip_tags(date_match.group(1)))

    return ""


def within_days(value: str, days: int) -> bool:
    try:
        item_date = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return True
    return item_date >= date.today() - timedelta(days=days)


def fetch_qbitai(days: int, logs: list[FetchLog]) -> list[Candidate]:
    url = f"https://www.qbitai.com/wp-json/wp/v2/posts?per_page=30&orderby=date&order=desc"
    try:
        payload = json.loads(fetch_text(url))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        logs.append(FetchLog("QbitAI WordPress", url, False, str(exc)))
        return []

    logs.append(FetchLog("QbitAI WordPress", url, True, f"{len(payload)} posts"))
    items: list[Candidate] = []
    for post in payload:
        published = parse_date(post.get("date", ""))
        if not within_days(published, days):
            continue
        title = strip_tags(post.get("title", {}).get("rendered", ""))
        excerpt = strip_tags(post.get("excerpt", {}).get("rendered", ""))
        content = strip_tags(post.get("content", {}).get("rendered", ""))[:3000]
        link = post.get("link", "")
        items.append(
            Candidate(
                title=title,
                source="量子位 / QbitAI",
                url=link,
                published_at=published,
                snippet=excerpt,
                body=content,
                evidence=[
                    {
                        "kind": "wechat_mirror",
                        "source": "量子位 / QbitAI",
                        "url": link,
                        "note": "QbitAI WordPress JSON 返回的公众号/媒体网页镜像。",
                    }
                ],
            )
        )
    return items


def fetch_sogou_wechat(account: str, query_terms: str, logs: list[FetchLog]) -> list[Candidate]:
    query = quote(f"{account} {query_terms}")
    url = f"https://weixin.sogou.com/weixin?type=2&query={query}"
    try:
        page = fetch_text(url)
    except (HTTPError, URLError, TimeoutError) as exc:
        logs.append(FetchLog(f"Sogou WeChat: {account}", url, False, str(exc)))
        return []

    blocks = re.findall(r'<div class="txt-box">(.*?)</li>', page, flags=re.S | re.I)
    candidates: list[Candidate] = []
    skipped_not_today = 0
    skipped_no_date = 0
    for block in blocks[:8]:
        title_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, flags=re.S | re.I)
        if not title_match:
            continue
        raw_url, raw_title = title_match.groups()
        item_url = urljoin("https://weixin.sogou.com", html.unescape(raw_url))
        title = strip_tags(raw_title)
        summary_match = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', block, flags=re.S | re.I)
        snippet = strip_tags(summary_match.group(1)) if summary_match else ""
        published = parse_sogou_date(block)
        if WECHAT_ONLY_TODAY and not published:
            skipped_no_date += 1
            continue
        if WECHAT_ONLY_TODAY and published != date.today().isoformat():
            skipped_not_today += 1
            continue
        candidates.append(
            Candidate(
                title=title,
                source=f"{account}（微信公众号线索）",
                url=item_url,
                published_at=published,
                snippet=snippet,
                body=snippet,
                evidence=[
                    {
                        "kind": "wechat_search",
                        "source": f"{account} / 搜狗微信",
                        "url": url,
                        "note": "通过搜狗微信网页搜索获得的公众号推送线索。",
                    }
                ],
            )
        )
    logs.append(
        FetchLog(
            f"Sogou WeChat: {account}",
            url,
            True,
            (
                f"{len(blocks)} result blocks, kept {len(candidates)}, "
                f"skipped_not_today {skipped_not_today}, skipped_no_date {skipped_no_date}"
            ),
        )
    )
    return candidates


def fetch_github(days: int, logs: list[FetchLog]) -> list[Candidate]:
    since = (date.today() - timedelta(days=days)).isoformat()
    queries = [
        f'"ai agent" pushed:>={since}',
        f'agentic pushed:>={since}',
        f'MCP pushed:>={since}',
        f'"coding agent" pushed:>={since}',
    ]
    repos: list[dict] = []
    seen_repo_urls: set[str] = set()
    for raw_query in queries:
        query = quote(raw_query)
        url = f"https://api.github.com/search/repositories?q={query}&sort=updated&order=desc&per_page=10"
        try:
            payload = json.loads(fetch_text(url))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
            logs.append(FetchLog("GitHub Search", url, False, str(exc)))
            continue
        batch = payload.get("items", [])
        logs.append(FetchLog("GitHub Search", url, True, f"{len(batch)} repos"))
        for repo in batch:
            repo_url = repo.get("html_url", "")
            if repo_url and repo_url not in seen_repo_urls:
                repos.append(repo)
                seen_repo_urls.add(repo_url)

    candidates: list[Candidate] = []
    for repo in repos[:12]:
        name = repo.get("full_name", "")
        description = repo.get("description") or ""
        repo_url = repo.get("html_url", "")
        updated = parse_date(repo.get("updated_at", ""))
        candidates.append(
            Candidate(
                title=f"GitHub 项目更新：{name}",
                source="GitHub Search",
                url=repo_url,
                published_at=updated,
                snippet=description,
                body=f"{name} {description}",
                evidence=[
                    {
                        "kind": "repository",
                        "source": "GitHub API",
                        "url": repo_url,
                        "note": f"仓库近期更新，stars={repo.get('stargazers_count', 0)}。",
                    }
                ],
            )
        )
    return candidates


def extract_links_from_page(
    source: str,
    page_url: str,
    include_pattern: str,
    days: int,
    logs: list[FetchLog],
    limit: int = 12,
) -> list[Candidate]:
    try:
        page = fetch_text(page_url)
    except (HTTPError, URLError, TimeoutError) as exc:
        logs.append(FetchLog(source, page_url, False, str(exc)))
        return []

    links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.S | re.I)
    logs.append(FetchLog(source, page_url, True, f"{len(links)} links"))
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for href, label in links:
        item_url = urljoin(page_url, html.unescape(href))
        if item_url in seen or not re.search(include_pattern, item_url, flags=re.I):
            continue
        title = strip_tags(label)
        if len(title) < 8:
            continue
        seen.add(item_url)
        candidates.append(
            Candidate(
                title=title,
                source=source,
                url=item_url,
                published_at=date.today().isoformat(),
                snippet="",
                evidence=[
                    {
                        "kind": "source_index",
                        "source": source,
                        "url": page_url,
                        "note": "从公开索引页抽取的候选文章链接。",
                    }
                ],
            )
        )
        if len(candidates) >= limit:
            break
    return candidates


def enrich_candidate(candidate: Candidate) -> Candidate:
    if candidate.body and len(candidate.body) > 500:
        return candidate
    parsed = urlparse(candidate.url)
    if parsed.netloc.endswith("sogou.com"):
        return candidate
    try:
        page = fetch_text(candidate.url, timeout=15)
    except (HTTPError, URLError, TimeoutError):
        return candidate
    page_text = clean_text(page)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.S | re.I)
    if title_match and (not candidate.title or len(candidate.title) < 8):
        candidate.title = strip_tags(title_match.group(1))
    candidate.body = page_text[:5000]
    candidate.evidence.append(
        {
            "kind": "secondary_web",
            "source": candidate.source,
            "url": candidate.url,
            "note": "二次抓取文章页面正文，用于分类、摘要和去重。",
        }
    )
    return candidate


def classify(candidate: Candidate) -> tuple[str, int]:
    text = f"{candidate.title} {candidate.snippet} {candidate.body}".lower()
    scores: dict[str, int] = {}
    for category, keywords in FOCUS_TOPICS.items():
        scores[category] = sum(text.count(keyword.lower()) for keyword in keywords)

    best_category, best_score = max(scores.items(), key=lambda item: item[1])
    source_bonus = 1 if any(name in candidate.source.lower() for name in ["qbitai", "量子位", "github", "challenge hub"]) else 0
    relevance = min(5, max(1, best_score + source_bonus))
    return best_category, relevance


def make_summary(candidate: Candidate, category: str) -> str:
    basis = candidate.snippet or candidate.body or candidate.title
    sentence = re.split(r"[。.!?？；;]", basis.strip())[0]
    if len(sentence) < 12:
        sentence = candidate.title
    sentence = clean_text(sentence)
    if len(sentence) > 92:
        sentence = sentence[:92].rstrip() + "..."
    if category not in sentence and len(sentence) < 85:
        sentence = f"{sentence}（{category}）"
    return sentence[:100]


def normalize_title(title: str) -> str:
    title = clean_text(title)
    title = re.sub(r"\s+", " ", title)
    if len(title) > 42:
        title = title[:42].rstrip() + "..."
    return title


def to_report_item(candidate: Candidate) -> dict | None:
    category, relevance = classify(candidate)
    if relevance < 3:
        return None
    return {
        "title": normalize_title(candidate.title),
        "source": candidate.source,
        "url": candidate.url,
        "date": candidate.published_at or date.today().isoformat(),
        "category": category,
        "summary": make_summary(candidate, category),
        "insight": INSIGHTS[category],
        "relevance": relevance,
        "evidence": candidate.evidence,
    }


def collect_candidates(days: int, logs: list[FetchLog]) -> list[Candidate]:
    candidates: list[Candidate] = []
    candidates.extend(fetch_qbitai(days, logs))
    candidates.extend(fetch_sogou_wechat("Challenge Hub", "AI Agent MCP 2026", logs))
    candidates.extend(fetch_sogou_wechat("量子位", "AI Agent MCP 2026", logs))
    candidates.extend(fetch_sogou_wechat("AI学习的老章", "AI Agent MCP 2026", logs))
    candidates.extend(fetch_github(days, logs))
    candidates.extend(
        extract_links_from_page(
            "Anthropic News",
            "https://www.anthropic.com/news",
            r"/news/",
            days,
            logs,
            limit=8,
        )
    )
    candidates.extend(
        extract_links_from_page(
            "Google DeepMind Blog",
            "https://deepmind.google/discover/blog/",
            r"/blog/",
            days,
            logs,
            limit=8,
        )
    )
    candidates.extend(
        extract_links_from_page(
            "OpenAI Blog",
            "https://openai.com/news/",
            r"/index/",
            days,
            logs,
            limit=8,
        )
    )
    candidates.extend(
        extract_links_from_page(
            "机器之心",
            "https://www.jiqizhixin.com/",
            r"jiqizhixin\.com/articles/",
            days,
            logs,
            limit=8,
        )
    )
    return candidates


def source_bucket(source: str) -> str:
    if "Challenge Hub" in source:
        return "Challenge Hub"
    if "量子位" in source or "QbitAI" in source:
        return "QbitAI"
    if "GitHub" in source:
        return "GitHub"
    if "Anthropic" in source:
        return "Anthropic"
    if "DeepMind" in source:
        return "DeepMind"
    if "OpenAI" in source:
        return "OpenAI"
    if "机器之心" in source:
        return "机器之心"
    return source


def dedupe_report_items(items: Iterable[dict], limit: int) -> list[dict]:
    selected: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    source_counts: dict[str, int] = {}
    ranked = sorted(items, key=lambda row: (row["relevance"], row["date"]), reverse=True)

    def can_add(item: dict, enforce_source_cap: bool) -> bool:
        url_key = item["url"].split("&token=")[0]
        title_key = re.sub(r"\W+", "", item["title"].lower())[:30]
        bucket = source_bucket(item["source"])
        if url_key in seen_urls or title_key in seen_titles:
            return False
        if enforce_source_cap and source_counts.get(bucket, 0) >= 3:
            return False
        return True

    def add_item(item: dict) -> None:
        url_key = item["url"].split("&token=")[0]
        title_key = re.sub(r"\W+", "", item["title"].lower())[:30]
        bucket = source_bucket(item["source"])
        selected.append(item)
        seen_urls.add(url_key)
        seen_titles.add(title_key)
        source_counts[bucket] = source_counts.get(bucket, 0) + 1

    for item in ranked:
        if can_add(item, enforce_source_cap=True):
            add_item(item)
        if len(selected) >= limit:
            return selected

    for item in ranked:
        if can_add(item, enforce_source_cap=False):
            add_item(item)
        if len(selected) >= limit:
            break
    return selected


def build_report(report_date: str | None, days: int, limit: int) -> dict:
    logs: list[FetchLog] = []
    candidates = collect_candidates(days, logs)

    report_items: list[dict] = []
    for candidate in candidates:
        time.sleep(0.1)
        enriched = enrich_candidate(candidate)
        item = to_report_item(enriched)
        if item:
            report_items.append(item)

    selected = dedupe_report_items(report_items, limit)
    if not selected:
        failed = "; ".join(f"{log.source}: {log.detail}" for log in logs if not log.ok)
        raise RuntimeError(f"No relevant live items were collected. Fetch failures: {failed}")

    return {
        "items": selected,
        "date": report_date or date.today().isoformat(),
        "topic_focus": "AI Agent、MCP、Context Engineering 与 AI Coding",
        "lead_sources": LEAD_SOURCES,
        "method": "真实抓取公众号搜索/媒体镜像/官方博客/GitHub API，随后二次抓取候选链接正文并按关键词分类、去重、摘要。",
        "run_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lookback_days": days,
            "candidates_collected": len(candidates),
            "items_after_filter": len(report_items),
            "fetch_logs": [asdict(log) for log in logs],
        },
    }


def default_report_path(report_date: str | None) -> Path:
    day = report_date or date.today().isoformat()
    return Path("reports") / f"{day}.json"


def write_report(output_path: Path, report_date: str | None, days: int, limit: int) -> None:
    report = build_report(report_date=report_date, days=days, limit=limit)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a live AI daily report.")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output JSON path. Defaults to reports/YYYY-MM-DD.json.",
    )
    parser.add_argument("--date", default=None, help="Report date in YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=10, help="Lookback window for recent content.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of report items.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = Path(args.output) if args.output else default_report_path(args.date)
    try:
        write_report(output_path, report_date=args.date, days=args.days, limit=args.limit)
    except Exception as exc:
        print(f"Failed to generate report: {exc}", file=sys.stderr)
        raise
    print(f"Generated {output_path}")


if __name__ == "__main__":
    main()
