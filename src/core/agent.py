"""src/core/agent.py - 逐条全链路 AI 技术日报生成器。

架构（v3.0 逐条全链路版）：
对每条候选文章，按以下顺序逐条处理：
  1. 抓取文章链接（微信公众号/GitHub/博客）
  2. 抓取原文全文（enrich：Selenium → urllib → fallback）
  3. LLM 阅读原文 → 生成 summary（80-120字中文摘要）+ insight（独立判断）
  4. LLM 语义分类
  5. AI 质量评估（是否值得写）
  6. LLM 写作（基于原文全文 + summary + insight，含模板腔规避约束）
  7. 审稿 Agent 评分（含模板腔检测）→ 不通过则逐轮修订（最多N轮）
  8. 保存文章 + 写入 report JSON item → 丢弃上下文 → 处理下一条

与旧版的关键区别：
- 不再批量抓取全部候选后再统一生成
- 不再分 agent → report → writer 三个阶段
- 每条文章的处理上下文在完成后立即丢弃
- 不需要中间文件 report JSON 做阶段间通信
- 模板腔规避和检测已交给 Agent，不再靠硬编码正则替换/匹配

微信公众号抓取策略：
- 优先使用 Selenium + Chrome 无头浏览器（可绕过搜狗反爬）
- Selenium 不可用时回退到 urllib（但成功率低）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# 关注领域及其关键词映射（需与 AGENT.md 保持一致）
# ---------------------------------------------------------------------------
FOCUS_TOPICS: dict[str, list[str]] = {
    "AI Agent": [
        "agent", "agentic", "智能体", "多智能体", "autonomous",
        "tool use", "工具调用", "workflow",
    ],
    "Harness Engineering": [
        "harness", "workflow", "orchestration", "评测", "benchmark",
        "pipeline", "系统工程", "可靠性",
    ],
    "Context Engineering": [
        "context", "上下文", "memory", "rag", "检索增强", "知识库",
    ],
    "MCP": ["mcp", "model context protocol", "server tooling", "connector"],
    "AI Coding": [
        "coding", "code", "codex", "claude code", "copilot",
        "github actions", "代码", "编程",
    ],
    "Vibe Coding": ["vibe coding", "氛围编程"],
    "LLM推理与优化": [
        "推理", "reasoning", "优化", "inference", "量化",
        "distillation", "蒸馏", "token", "latency", "延迟",
    ],
    "多模态大模型": [
        "多模态", "multimodal", "视觉", "vision", "语音", "audio",
        "视频", "video", "图像生成", "image generation",
    ],
}

# 微信公众号来源（通过搜狗微信搜索）
# 配置在 src/constants/wechat_sources.py 中，此处从常量模块导入
from src.constants.wechat_sources import WECHAT_SOURCES, WECHAT_ACCOUNT_IDS
from src.constants.info_sources import (
    INSIGHTS,
    get_web_sources,
    get_wp_api_sources,
    build_github_url,
    get_source_bucket,
    get_insight,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent.parent.parent
REPORTS_DIR = ROOT / "reports"
PAPER_DIR = ROOT / "daily_paper"

# 输出数量约束
TARGET_ARTICLES = 5
MAX_GITHUB = 2
MIN_NON_GITHUB = 3


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    title: str
    source: str
    url: str
    published_at: str = ""
    snippet: str = ""
    body: str = ""


@dataclass
class FetchLog:
    source: str
    url: str
    ok: bool
    detail: str


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def fetch_text(url: str, timeout: int = 18) -> str:
    """抓取指定 URL 的文本内容。"""
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
    """清洗 HTML 文本。"""
    import html as _html
    value = _html.unescape(value or "")
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def parse_sogou_date(block: str) -> str | None:
    """从搜狗微信搜索结果块中提取日期。"""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", block)
    if match:
        return match.group(1)
    return None


def is_sogou_antispider_page(html_text: str) -> bool:
    lowered = html_text.lower()
    return any(token in lowered for token in ("antispider", "verify", "imgcode", "captcha"))


def normalize_sogou_link(href: str) -> str:
    from html import unescape
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    absolute = urljoin("https://weixin.sogou.com/", unescape(href))
    parts = urlsplit(absolute)
    query = urlencode(parse_qsl(parts.query, keep_blank_values=True))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


# ---------------------------------------------------------------------------
# 抓取函数
# ---------------------------------------------------------------------------

def fetch_sogou_wechat(account_name: str, query: str, logs: list[FetchLog], days: int = 7) -> list[Candidate]:
    """通过 Selenium 浏览器抓取搜狗微信搜索的文章。

    优先使用 Selenium + Chrome 无头浏览器（绕过反爬），
    失败时回退到传统 urllib 方式（成功率低）。

    自动过滤超出 days 回溯窗口的旧文章。
    """
    cutoff_date = (datetime.now() - timedelta(days=days)).date()
    candidates: list[Candidate] = []

    # 策略 1: Selenium 浏览器抓取（推荐）
    try:
        from src.infrastructure.browser_fetcher import fetch_wechat_source_full
        articles = fetch_wechat_source_full(
            account_name=account_name,
            query=query,
            max_articles=15,  # 翻页抓取更多，由 agent.py 日期过滤筛选当日文章
            headless=True,
            days=days,
        )
        for article in articles:
            published_at = article.get("published_at", "")
            # 日期过滤：只保留 days 天内的文章
            if published_at:
                try:
                    pub_date = datetime.strptime(published_at, "%Y-%m-%d").date()
                    if pub_date < cutoff_date:
                        print(f"  [FILTER] 过期文章（{published_at}），跳过: {article['title'][:40]}...")
                        continue
                except ValueError:
                    pass
            candidates.append(
                Candidate(
                    title=article["title"],
                    source=article["source"],
                    url=article["url"],
                    published_at=published_at,
                    snippet=article.get("snippet", ""),
                    body=article.get("body", ""),
                )
            )
            logs.append(
                FetchLog(
                    source=account_name,
                    url=article["url"],
                    ok=True,
                    detail=f"Selenium: title={article['title'][:40]}, body={len(article.get('body', ''))} chars",
                )
            )
        if candidates:
            return candidates
    except Exception as exc:
        logs.append(
            FetchLog(
                source=account_name,
                url="Selenium browser",
                ok=False,
                detail=f"Selenium failed: {exc}, falling back to urllib",
            )
        )

    # 策略 2: 回退到 urllib + 搜索引擎（旧方案，成功率低）
    return _fetch_sogou_wechat_urllib(account_name, query, logs)


def _fetch_sogou_wechat_urllib(account_name: str, query: str, logs: list[FetchLog]) -> list[Candidate]:
    """旧版 urllib 抓取方案（回退策略）。

    搜狗微信搜索已改为 JS 动态加载，纯 HTTP 抓取无法获取完整结果。
    因此改用替代策略：通过搜索引擎搜索 mp.weixin.qq.com 上的文章。
    """
    candidates: list[Candidate] = []

    # 策略 A: 直接搜 mp.weixin.qq.com + 公众号名（通过搜狗通用搜索）
    search_queries = [
        f"site:mp.weixin.qq.com {account_name} AI",
        f"{account_name} 公众号 mp.weixin.qq.com",
    ]

    for q in search_queries:
        if len(candidates) >= 3:
            break
        search_url = f"https://www.sogou.com/web?query={quote(q)}"
        try:
            html_text = fetch_text(search_url, timeout=20)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            logs.append(FetchLog(source=account_name, url=search_url, ok=False, detail=str(exc)))
            continue
        if is_sogou_antispider_page(html_text):
            logs.append(
                FetchLog(
                    source=account_name,
                    url=search_url,
                    ok=False,
                    detail="Sogou anti-spider verification page",
                )
            )
            continue

        wechat_urls = re.findall(
            r'https?://mp\.weixin\.qq\.com/s/[^"&\s]+',
            html_text,
        )
        seen_urls: set[str] = set()
        for url in wechat_urls[:5]:
            if url in seen_urls:
                continue
            seen_urls.add(url)

            idx = html_text.find(url)
            context_block = html_text[max(0, idx - 500):idx + 200]
            title = ""
            for pattern in [
                r'<a[^>]*>\s*<em>(.*?)</em>',
                r'title="([^"]*)"',
                r'<h3[^>]*>(.*?)</h3>',
                r'>([^<]{10,80})<',
            ]:
                title_match = re.search(pattern, context_block, re.S)
                if title_match:
                    title = clean_text(title_match.group(1))[:80]
                    if len(title) >= 5:
                        break

            if not title or len(title) < 3:
                title = f"{account_name} 文章"

            pub_date = parse_sogou_date(context_block) or ""

            candidates.append(
                Candidate(
                    title=title,
                    source=f"{account_name}（微信公众号）",
                    url=url,
                    published_at=pub_date,
                    snippet=clean_text(context_block)[:200],
                )
            )
            logs.append(FetchLog(source=account_name, url=url, ok=True, detail=f"urllib: title={title[:40]}"))

    # 策略 B: Bing 搜索
    if not candidates:
        try:
            bing_url = f"https://www.bing.com/search?q=site:mp.weixin.qq.com+{quote(account_name)}+AI&count=5"
            html_text = fetch_text(bing_url, timeout=20)
            wechat_urls = re.findall(
                r'https?://mp\.weixin\.qq\.com/s/[^"&\s]+',
                html_text,
            )
            seen_urls: set[str] = set()
            for url in wechat_urls[:5]:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                idx = html_text.find(url)
                context_block = html_text[max(0, idx - 300):idx + 200]
                title_match = re.search(r'<h2[^>]*>(.*?)</h2>', context_block, re.S)
                title = clean_text(title_match.group(1)) if title_match else f"{account_name} 文章"
                candidates.append(
                    Candidate(
                        title=title[:80],
                        source=f"{account_name}（微信公众号）",
                        url=url,
                        snippet=clean_text(context_block)[:200],
                    )
                )
            logs.append(FetchLog(source=account_name, url=bing_url, ok=True, detail=f"Bing found {len(candidates)} articles"))
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            logs.append(FetchLog(source=account_name, url="bing search", ok=False, detail=str(exc)))

    return candidates


def fetch_sogou_wechat_legacy(account_name: str, query: str, logs: list[FetchLog]) -> list[Candidate]:
    """使用搜狗微信老入口 weixin.sogou.com/weixin?type=2 抓取文章。"""
    candidates: list[Candidate] = []
    search_query = f"{account_name} {query} MCP 2026"
    search_url = f"https://weixin.sogou.com/weixin?type=2&query={quote(search_query)}"
    try:
        html_text = fetch_text(search_url, timeout=20)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        logs.append(FetchLog(source=f"Sogou WeChat: {account_name}", url=search_url, ok=False, detail=str(exc)))
        return candidates

    if is_sogou_antispider_page(html_text):
        logs.append(
            FetchLog(
                source=f"Sogou WeChat: {account_name}",
                url=search_url,
                ok=False,
                detail="Sogou anti-spider verification page",
            )
        )
        return candidates

    blocks = re.findall(
        r'(<li id="sogou_vr_11002601_box_\d+".*?</li>)',
        html_text,
        flags=re.S,
    )
    skipped_no_date = 0
    for block in blocks:
        title_match = re.search(r'id="sogou_vr_11002601_title_\d+"[^>]*>(.*?)</a>', block, re.S)
        # href 可能在 id 前面或后面，用更宽松的匹配
        href_match = re.search(r'href="(/link\?url=[^"]+)"', block, re.S)
        summary_match = re.search(r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.S)

        if not title_match or not href_match:
            continue

        # 搜狗新格式: timeConvert('1771042006') 单引号
        block_start = html_text.find(block)
        surrounding = html_text[block_start:block_start + 5000]
        ts_match = re.search(r"timeConvert\(\s*['\"]?(?P<ts>\d{10})", surrounding)
        if ts_match:
            published_at = datetime.fromtimestamp(int(ts_match.group("ts"))).strftime("%Y-%m-%d")
        else:
            # 从标题和摘要中尝试提取中文日期格式
            combined = clean_text(title_match.group(1)) + " " + clean_text(summary_match.group(1) if summary_match else "")
            date_match = re.search(r"(\d{4})[\u5e74\-\/](\d{1,2})[\u6708\-\/](\d{1,2})[\u65e5]?", combined)
            if date_match:
                published_at = f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
            else:
                # 只匹配年份的宽松策略
                year_match = re.search(r"(\d{4})[\u5e74]", combined)
                if year_match:
                    published_at = f"{year_match.group(1)}-01-01"
                else:
                    skipped_no_date += 1
                    continue

        title = clean_text(title_match.group(1))[:80] or f"{account_name} 文章"
        snippet = clean_text(summary_match.group(1))[:200] if summary_match else ""
        candidates.append(
            Candidate(
                title=title,
                source=f"{account_name}（微信公众号）",
                url=normalize_sogou_link(href_match.group(1)),
                published_at=published_at,
                snippet=snippet,
            )
        )
        if len(candidates) >= 5:
            break

    logs.append(
        FetchLog(
            source=f"Sogou WeChat: {account_name}",
            url=search_url,
            ok=True,
            detail=f"{len(blocks)} result blocks, kept {len(candidates)}, skipped_no_date {skipped_no_date}",
        )
    )
    return candidates


def fetch_wp_api_sources(days: int, logs: list[FetchLog]) -> list[Candidate]:
    """从 WordPress API 源抓取文章（如 QbitAI 等）。"""
    candidates: list[Candidate] = []
    for wp_source in get_wp_api_sources():
        wp_name = wp_source["name"]
        wp_url = wp_source["url"]
        wp_display = wp_source.get("display_name", wp_name)
        wp_limit = wp_source.get("limit", 6)
        try:
            response = fetch_text(wp_url, timeout=20)
            posts = json.loads(response)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            logs.append(FetchLog(source=wp_name, url=wp_url, ok=False, detail=str(exc)))
            continue

        cutoff = datetime.now() - timedelta(days=days)
        for post in posts:
            published_raw = str(post.get("date", ""))
            published_at = published_raw[:10]
            try:
                published_dt = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            except ValueError:
                published_dt = None
            if published_dt and published_dt < cutoff:
                continue

            title = clean_text(post.get("title", {}).get("rendered", ""))
            excerpt = clean_text(post.get("excerpt", {}).get("rendered", ""))
            link = post.get("link", "")
            if not title or not link:
                continue

            candidates.append(
                Candidate(
                    title=title[:80],
                    source=wp_display,
                    url=link,
                    published_at=published_at,
                    snippet=excerpt[:200],
                    body=clean_text(post.get("content", {}).get("rendered", ""))[:4000],
                )
            )
            if len(candidates) >= wp_limit:
                break

        logs.append(FetchLog(source=wp_name, url=wp_url, ok=True, detail=f"found {len(candidates)} posts"))
    return candidates


def fetch_github(days: int, logs: list[FetchLog]) -> list[Candidate]:
    """从 GitHub 搜索 AI 相关热门仓库。"""
    from src.constants.info_sources import GITHUB_SOURCE_NAME
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = build_github_url(since)
    candidates: list[Candidate] = []
    try:
        response = fetch_text(url, timeout=20)
        data = json.loads(response)
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logs.append(FetchLog(source=GITHUB_SOURCE_NAME, url=url, ok=False, detail=str(exc)))
        return candidates
    for repo in data.get("items", [])[:MAX_GITHUB]:
        candidates.append(
            Candidate(
                title=repo.get("full_name", ""),
                source=GITHUB_SOURCE_NAME,
                url=repo.get("html_url", ""),
                published_at=repo.get("created_at", "")[:10],
                snippet=repo.get("description", "") or "",
            )
        )
    logs.append(FetchLog(source=GITHUB_SOURCE_NAME, url=url, ok=True, detail=f"found {len(candidates)} repos"))
    return candidates


def fetch_blog_links(
    source: str, page_url: str, pattern: str, logs: list[FetchLog], limit: int = 5,
) -> list[Candidate]:
    """从博客/媒体页面提取文章链接。"""
    candidates: list[Candidate] = []
    try:
        html_text = fetch_text(page_url, timeout=20)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        logs.append(FetchLog(source=source, url=page_url, ok=False, detail=str(exc)))
        return candidates

    # 更精确的链接匹配：排除 RSS、CSS、JS 等非文章链接
    links = re.findall(
        rf'href="(https?://[^"]*{pattern}[^"]*)"',
        html_text, flags=re.I,
    )
    # 过滤非文章链接
    links = [
        link for link in links
        if not any(skip in link.lower() for skip in ["rss", ".css", ".js", ".png", ".jpg", ".ico", "cdn."])
    ]

    seen: set[str] = set()
    for link in links[:limit]:
        if link in seen:
            continue
        seen.add(link)

        # 在链接周围提取标题
        idx = html_text.find(link)
        surrounding = html_text[max(0, idx - 600):idx + 400]

        title = ""
        # 尝试多种标题提取策略
        for pattern_re in [
            r'<h\d[^>]*>(.*?)</h\d>',       # 标题标签
            r'(?:title|aria-label)="([^"]*)"', # title/aria-label 属性
            r'<a[^>]*>\s*(.+?)\s*</a>',       # 链接文本
        ]:
            title_match = re.search(pattern_re, surrounding, re.S | re.I)
            if title_match:
                candidate_title = clean_text(title_match.group(1))[:100]
                # 确保标题有意义
                if len(candidate_title) >= 5 and not candidate_title.startswith("http"):
                    title = candidate_title
                    break

        if not title or len(title) < 3:
            # 从 URL 提取有意义的部分
            parts = link.rstrip("/").split("/")
            title = parts[-1].replace("-", " ").replace("_", " ")[:80] if parts else source
            # 如果还是太短，用倒数第二个路径段
            if len(title) < 5 and len(parts) > 1:
                title = parts[-2].replace("-", " ").replace("_", " ")[:80]

        title = clean_text(title)[:80]
        if len(title) < 3:
            title = f"{source} 文章"

        candidates.append(Candidate(title=title, source=source, url=link))

    logs.append(FetchLog(source=source, url=page_url, ok=True, detail=f"extracted {len(candidates)} links"))
    return candidates


def enrich_candidate(candidate: Candidate) -> None:
    """抓取候选文章的原文全文。

    如果 body 已存在（Selenium 已抓取）且足够长，则跳过。
    否则尝试多种策略获取原文：
    1. Selenium 浏览器抓取（微信文章）
    2. urllib 直接抓取
    3. 使用 snippet 作为 fallback
    """
    if candidate.body and len(candidate.body) >= 300:
        return
    url = candidate.url

    # 微信文章：尝试用 Selenium 浏览器抓取
    if "mp.weixin.qq.com" in url:
        try:
            from src.infrastructure.browser_fetcher import (
                create_browser, fetch_wechat_article,
            )
            with create_browser(headless=True) as driver:
                body = fetch_wechat_article(driver, url)
                if body and len(body) >= 300:
                    candidate.body = body
                    return
        except Exception as exc:
            print(f"  [WARN] Selenium 抓取微信文章失败: {exc}")

    # 非微信 URL 或微信 Selenium 失败：urllib 抓取
    try:
        candidate.body = clean_text(fetch_text(url, timeout=25))
        if candidate.body and len(candidate.body) >= 300:
            return
    except Exception as exc:
        print(f"  [WARN] urllib 抓取失败: {exc}")

    # 最终 fallback：使用 snippet 或已有 body
    if not candidate.body:
        candidate.body = candidate.snippet or ""


def classify(candidate: Candidate) -> str:
    """根据标题和正文对候选文章进行分类。

    优先使用 LLM 做语义分类，LLM 不可用时回退到关键词匹配。
    """
    # 优先使用 LLM 分类
    try:
        from src.infrastructure.llm_client import OpenAICompatibleProvider
        # 尝试创建 LLM provider（不抛异常则可用）
        provider = _get_shared_llm()
        if provider:
            return _classify_with_llm(candidate, provider)
    except Exception:
        pass

    # 回退：关键词匹配
    text = f"{candidate.title} {candidate.snippet} {candidate.body[:2000]}".lower()
    for topic, keywords in FOCUS_TOPICS.items():
        if any(keyword in text for keyword in keywords):
            return topic
    return "AI Agent"


def _classify_with_llm(candidate: Candidate, provider) -> str:
    """使用 LLM 对文章进行语义分类。"""
    valid_categories = list(FOCUS_TOPICS.keys())
    categories_list = "\n".join(f"- {c}" for c in valid_categories)

    prompt = [
        {
            "role": "system",
            "content": (
                "你是一名技术编辑。请根据文章标题和摘要，判断它最接近哪个技术领域。"
                "只回复分类名称，不要加任何解释。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"文章标题：{candidate.title}\n"
                f"摘要/简介：{candidate.snippet[:300]}\n\n"
                f"可选分类：\n{categories_list}\n\n"
                f"请选择最匹配的一个分类："
            ),
        },
    ]

    try:
        response = provider.chat(prompt, temperature=0.1, max_tokens=30)
        response = response.strip()
        # 从回复中提取有效分类名
        for cat in valid_categories:
            if cat in response:
                return cat
        return "AI Agent"
    except Exception:
        return "AI Agent"


# 缓存的 LLM provider 实例
_shared_llm_provider = None


def _get_shared_llm():
    """获取共享的 LLM provider（用于 agent.py 内的 AI 摘要/分类/洞察生成）。"""
    global _shared_llm_provider
    if _shared_llm_provider is not None:
        return _shared_llm_provider
    try:
        from src.infrastructure.llm_client import create_llm_provider
        _shared_llm_provider = create_llm_provider()
        return _shared_llm_provider
    except Exception:
        return None


def normalize_title(title: str) -> str:
    """规范化标题。"""
    title = re.sub(r"【[^】]*】", "", title)
    title = re.sub(r"\s+", " ", title)
    return title.strip()[:80]


# ---------------------------------------------------------------------------
# 主流程：抓取 + 逐篇评估 + 生成
# ---------------------------------------------------------------------------

def collect_candidates(days: int, logs: list[FetchLog]) -> list[Candidate]:
    """收集所有候选文章。

    抓取策略：
    - 微信公众号（通过搜索引擎搜索 mp.weixin.qq.com）
      注意：搜狗微信搜索已改为 JS 动态加载，纯 HTTP 抓取可能获取不到结果。
      此时系统会从其他来源补足。
    - GitHub 最多 2 篇
    - 网页信息源（配置在 src/constants/info_sources.py）
    - WordPress API 源（配置在 src/constants/info_sources.py）
    """
    all_candidates: list[Candidate] = []

    # 1. 微信公众号（通过 Selenium 浏览器抓取搜狗微信搜索，失败时回退 urllib）
    for account_name, query in WECHAT_SOURCES:
        wechat_candidates = fetch_sogou_wechat(account_name, query, logs, days=days)
        all_candidates.extend(wechat_candidates)
        time.sleep(1)

    # 2. GitHub（最多 2 篇）
    github_candidates = fetch_github(days, logs)
    all_candidates.extend(github_candidates[:MAX_GITHUB])

    # 3. 网页信息源（从常量配置读取）
    for source_name, source_url, url_pattern in get_web_sources():
        all_candidates.extend(
            fetch_blog_links(source_name, source_url, url_pattern, logs, limit=5)
        )

    # 4. WordPress API 源
    all_candidates.extend(fetch_wp_api_sources(days, logs))

    return all_candidates


def to_report_item(candidate: Candidate) -> dict:
    """将候选文章转为报告条目。

    v2.4 改进：使用 LLM 生成高质量的 summary 和 insight，
    不再用截断原文和硬编码模板。
    """
    category = classify(candidate)

    # 用 LLM 生成 AI 摘要和洞察
    summary, insight = _generate_ai_summary_and_insight(candidate, category)

    # 确保 relevance 至少为 3（验证脚本要求 >= 3）
    body_len = len(candidate.body or "")
    snippet_len = len(candidate.snippet or "")
    relevance = max(3, min(5, 3 + (body_len + snippet_len) // 600))
    return {
        "title": normalize_title(candidate.title),
        "source": candidate.source,
        "url": candidate.url,
        "date": candidate.published_at or date.today().isoformat(),
        "category": category,
        "summary": summary,
        "insight": insight,
        "relevance": relevance,
    }


def _generate_ai_summary_and_insight(candidate: Candidate, category: str) -> tuple[str, str]:
    """使用 LLM 为候选文章生成高质量的摘要和洞察。

    返回 (summary, insight) 元组。
    LLM 不可用时回退到旧逻辑。
    """
    provider = _get_shared_llm()
    if not provider:
        # LLM 不可用，回退到旧逻辑
        fallback_summary = (candidate.snippet or candidate.body)[:100]
        fallback_insight = INSIGHTS.get(category, INSIGHTS["AI Agent"])[:150]
        return fallback_summary, fallback_insight

    # 构建原文内容（优先用 body，其次 snippet）
    content = (candidate.body or candidate.snippet or "")[:5000]

    if len(content) < 100:
        # 内容太少，不值得调 LLM
        return (candidate.snippet or candidate.title)[:100], INSIGHTS.get(category, INSIGHTS["AI Agent"])[:150]

    prompt = [
        {
            "role": "system",
            "content": (
                "你是一名资深技术编辑。请阅读下面的文章内容，生成两段文字：\n\n"
                "1. **summary**（80-120字）：用简洁中文概括文章的核心内容，"
                "要说清楚是什么事件/技术/产品，关键信息是什么。不要写\"本文介绍了\"这种套话。\n\n"
                "2. **insight**（40-80字）：给出你对这条内容的独立判断——"
                "这件事为什么值得关注？技术或产业上有什么信号？判断要克制、有分寸。"
                "不要写成\"值得关注\"这种空话，要说出具体原因。\n\n"
                "请严格按以下格式输出，不要加任何其他内容：\n"
                "SUMMARY: <摘要内容>\n"
                "INSIGHT: <洞察内容>"
            ),
        },
        {
            "role": "user",
            "content": (
                f"文章标题：{candidate.title}\n"
                f"来源：{candidate.source}\n"
                f"分类：{category}\n\n"
                f"文章内容：\n{content}"
            ),
        },
    ]

    try:
        response = provider.chat(prompt, temperature=0.4, max_tokens=400)
        response = response.strip()

        # 解析 SUMMARY 和 INSIGHT
        summary_match = re.search(r"SUMMARY:\s*(.+?)(?:\n|$)", response, re.S)
        insight_match = re.search(r"INSIGHT:\s*(.+?)(?:\n|$)", response, re.S)

        summary = summary_match.group(1).strip() if summary_match else ""
        insight = insight_match.group(1).strip() if insight_match else ""

        # 如果解析失败，尝试用整段文本分割
        if not summary or not insight:
            lines = response.split("\n")
            for line in lines:
                line = line.strip()
                if line.upper().startswith("SUMMARY") and not summary:
                    summary = line.split(":", 1)[-1].strip()
                elif line.upper().startswith("INSIGHT") and not insight:
                    insight = line.split(":", 1)[-1].strip()

        # 最终 fallback：LLM 已返回内容但标签解析失败，
        # 用原始响应的前 200 字符作为 insight，保留 LLM 的判断
        # 而不是回退到死板的 INSIGHTS 模板
        if not summary:
            summary = (candidate.snippet or candidate.body)[:100]
        if not insight:
            # 优先用 LLM 原始响应（去掉 SUMMARY: 前缀），它至少有自己的判断
            raw_insight = re.sub(r"^\s*SUMMARY:\s*", "", response).strip()
            insight = raw_insight[:200] if raw_insight else (candidate.snippet or candidate.body)[:100]

        return summary[:200], insight[:200]

    except Exception:
        # LLM 调用失败，回退
        return (candidate.snippet or candidate.body)[:100], INSIGHTS.get(category, INSIGHTS["AI Agent"])[:150]


def source_bucket(source: str) -> str:
    """判断来源类型（委托给常量模块）。"""
    return get_source_bucket(source)


# ---------------------------------------------------------------------------
# v2.5: 逐条全链路处理 —— 抓取→阅读→写作→审稿→修订→保存
# ---------------------------------------------------------------------------

def infer_article_type(item: dict) -> str:
    """根据条目信息推断文章类型。"""
    from src.constants.info_sources import GITHUB_TYPE_KEYWORDS, TOOL_TYPE_KEYWORDS
    source = str(item.get("source", ""))
    title = str(item.get("title", ""))
    url = str(item.get("url", ""))
    text = f"{source} {title} {url}".lower()
    if any(kw in text for kw in GITHUB_TYPE_KEYWORDS):
        return "工具型"
    if any(word in text for word in TOOL_TYPE_KEYWORDS):
        return "解读型"
    return "主线型"


def article_title(item: dict) -> str:
    """根据条目生成文章标题。"""
    title = str(item.get("title", "")).strip(" .")
    compact = re.sub(r"GitHub 项目更新：", "", title)
    compact = compact.replace("！", "").replace("？", "")
    return compact[:50]


def unique_output_file(day: str, title: str, item: dict, used_outputs: set[str]) -> str:
    """为文章生成唯一的输出文件路径。"""
    from src.common.utils import slugify

    base = slugify(title)
    output = f"daily_paper/{day}-{base}.md"
    if output not in used_outputs:
        used_outputs.add(output)
        return output

    # 如果有冲突，加来源短名
    source_hint = slugify(str(item.get("source", ""))[:18], max_len=18)
    output = f"daily_paper/{day}-{base}-{source_hint}.md"
    counter = 2
    while output in used_outputs:
        output = f"daily_paper/{day}-{base}-{source_hint}-{counter}.md"
        counter += 1
    used_outputs.add(output)
    return output


def evaluate_quality_with_ai(
    candidate: Candidate,
    llm_provider,
) -> tuple[bool, str]:
    """用 AI 评估候选文章是否值得写。

    返回 (是否通过, 评估理由)。
    """
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
                f"标题：{candidate.title}\n"
                f"来源：{candidate.source}\n"
                f"URL：{candidate.url}\n\n"
                f"原文内容（前 3000 字）：\n{(candidate.body or candidate.snippet or '')[:3000]}"
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
        return True, f"AI评估异常({exc})，默认通过"


def build_llm_writer_prompt(item: dict, original_text: str) -> list[dict]:
    """构建 LLM 写作 prompt。"""
    article_type = item.get("article_type", "")
    summary = item.get("summary", "")
    insight = item.get("insight", "")
    source_title = item.get("title", "")
    url = item.get("url", "")

    github_note = ""
    if article_type == "工具型" and "github.com/" in url.lower():
        github_note = f"- 标题下方保留项目链接：{url}\n"

    system = (
        "你是一名技术公众号编辑，擅长把技术动态写成有判断、有细节、读起来像真人写的文章。\n\n"
        "核心原则：\n"
        "1. 从原文中提取具体事实作为文章骨架——人名、项目名、数据、时间、技术术语\n"
        "2. 每个二级标题下必须有原文中的具体信息，不要写成空泛的「XX的意义」\n"
        "3. 段落要自然——事实→解释→判断，不要用「首先其次最后」串段落\n"
        "4. 语言像技术博客，不像新闻稿也不像论文摘要\n"
        "5. 只写原文里有的内容，不要脑补，信息不足就降低判断强度\n\n"
        "避免以下表达（它们是常见模板腔，会让文章显得像 AI 生成的）：\n"
        "- 「真正值得看的不是」「真正值得看的，不是」「真正值得」「真正考验」\n"
        "- 「背后的技术信号」「这条动态值得拆开看」\n"
        "- 「在当今时代」「随着AI的快速发展」「众所周知」「综上所述」「总而言之」\n"
        "- 任何「不是…而是…」的对比句式作为开场\n"
    )

    type_hint = ""
    if article_type == "主线型":
        type_hint = "这篇文章是主线型——围绕一个具体事件展开。先讲清楚谁做了什么、结果如何，再解释这件事改变了什么。\n"
    elif article_type == "解读型":
        type_hint = "这篇文章是解读型——围绕一个技术趋势展开。用原文中的具体事实做证据，解释变化背后的原因，判断要克制。\n"
    elif article_type == "工具型":
        type_hint = "这篇文章是工具型——围绕一个工具/项目展开。说清它解决什么问题、怎么用、局限在哪，少写宣传腔。\n"

    user = f"""请基于下面的原文全文，写一篇中文技术公众号文章。

{type_hint}
格式要求：
- 一级标题作为文章标题，要像一个独立文章的标题
- 至少 3 个二级标题，每个二级标题中必须包含原文的具体信息
- 正文 1300-1800 字
- 段落 2-4 句，不单句成段
- 至少一处自然的具体例子或类比
{github_note}
参考信息：
- AI 摘要：{summary}
- 编辑洞察：{insight}
- 原标题（仅供参考）：{source_title}

原文全文：
{original_text[:120000]}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def polish_llm_article(content: str) -> str:
    """对 LLM 生成的文章进行轻量润色（仅做段落拆分，不再硬编码模板替换）。"""
    return wrap_long_paragraphs(content)


def wrap_long_paragraphs(article: str) -> str:
    """将过长段落按句拆分。"""
    from src.common.utils import chinese_char_count as ccc

    wrapped: list[str] = []
    for paragraph in article.split("\n\n"):
        stripped = paragraph.strip()
        if not stripped or stripped.startswith("#") or ccc(stripped) <= 170:
            wrapped.append(paragraph)
            continue
        sentences = re.split(r"(?<=[。！？])", stripped)
        current = ""
        chunks: list[str] = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if current and ccc(current + sentence) > 150:
                chunks.append(current.strip())
                current = sentence
            else:
                current += sentence
        if current:
            chunks.append(current.strip())
        wrapped.append("\n\n".join(chunks))
    return "\n\n".join(wrapped) + "\n"


def review_article(
    article_text: str,
    article_type: str,
    title: str,
    source_title: str,
    source_url: str,
    reviewer,
) -> dict:
    """用审稿 Agent 对文章做综合质量评估。"""
    system = (
        "你是一名资深技术编辑和审稿人。你的任务是审读一篇公众号技术文章，"
        "从多个维度给出客观评价和具体修改建议。\n\n"
        "审稿原则：\n"
        "- 你不是在检查清单，而是在判断「这篇文章像不像一个真人编辑写出来的」\n"
        "- 重点看：有没有自己的判断视角、段落之间有没有自然的起承转合、"
        "二级标题是不是有具体信息而不是空泛概括、有没有模板腔和套话\n"
        "- 评分要公正客观，不要因为文章格式规整就自动压低分数\n"
    )

    user = f"""请审读下面这篇技术公众号文章，给出评分和修改建议。

文章类型：{article_type}
文章标题：{title}
原文标题：{source_title}
原文链接：{source_url}

请从以下五个维度分别打分（每项 0-10 分），并给出 1-2 句简短评语：

1. **hook（开头吸引力）**：开头 200 字是否快速抓住读者？
2. **point_of_view（观点判断力）**：文章是否有自己明确的判断？
3. **storyline（结构推进感）**：二级标题是否包含具体信息？段落之间是否有自然的起承转合？
4. **technical_depth（技术深度）**：是否有具体的工程概念、技术术语、使用场景？
5. **wechat_readability（公众号可读性）**：段落长度是否适合手机阅读？语言是否自然？

然后给出：
- **overall_score**：总分 0-100（五项各 0-10，总分 = 五项之和 × 2）
- **verdict**：PASS（>=65 分）或 FAIL（<65 分）
- **suggestions**：2-5 条具体修改建议（中文，每条一句话，要具体可操作）

请严格按以下 JSON 格式输出：
```json
{{
  "hook": {{"score": 8, "comment": "..."}},
  "point_of_view": {{"score": 7, "comment": "..."}},
  "storyline": {{"score": 6, "comment": "..."}},
  "technical_depth": {{"score": 7, "comment": "..."}},
  "wechat_readability": {{"score": 8, "comment": "..."}},
  "overall_score": 72,
  "verdict": "PASS",
  "suggestions": ["建议1", "建议2"]
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
            "score": 0, "checks": {}, "suggestions": [f"审稿 Agent 调用失败: {exc}"],
            "passed": False, "raw_response": str(exc),
        }

    # 解析 JSON 响应
    try:
        json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.S)
        json_str = (json_match.group(1) if json_match else response).strip()
        brace_start = json_str.find("{")
        brace_end = json_str.rfind("}")
        if brace_start != -1 and brace_end != -1:
            json_str = json_str[brace_start:brace_end + 1]
        data = json.loads(json_str)
    except (json.JSONDecodeError, ValueError):
        return {
            "score": 0, "checks": {},
            "suggestions": [f"审稿 Agent 返回格式异常: {response[:300]}"],
            "passed": False, "raw_response": response,
        }

    score = int(data.get("overall_score", 0))
    checks = {
        "hook": {"score": int(data.get("hook", {}).get("score", 5)), "comment": str(data.get("hook", {}).get("comment", ""))},
        "point_of_view": {"score": int(data.get("point_of_view", {}).get("score", 5)), "comment": str(data.get("point_of_view", {}).get("comment", ""))},
        "storyline": {"score": int(data.get("storyline", {}).get("score", 5)), "comment": str(data.get("storyline", {}).get("comment", ""))},
        "technical_depth": {"score": int(data.get("technical_depth", {}).get("score", 5)), "comment": str(data.get("technical_depth", {}).get("comment", ""))},
        "wechat_readability": {"score": int(data.get("wechat_readability", {}).get("score", 5)), "comment": str(data.get("wechat_readability", {}).get("comment", ""))},
    }
    suggestions = [str(s) for s in data.get("suggestions", [])]
    verdict = str(data.get("verdict", "FAIL")).upper()
    passed = verdict == "PASS" and score >= 65

    return {
        "score": max(0, min(100, score)),
        "checks": checks,
        "suggestions": suggestions,
        "passed": passed,
        "raw_response": response,
    }


def revise_article_with_feedback(
    article_text: str,
    original_text: str,
    item: dict,
    review_result: dict,
    round_num: int,
    llm_provider,
) -> str:
    """根据审稿 Agent 的反馈修改文章。"""
    article_type = item.get("article_type", "")
    title = item.get("title", "")
    suggestions_text = "\n".join(f"- {s}" for s in review_result.get("suggestions", []))
    checks_text = ""
    for name, detail in review_result.get("checks", {}).items():
        if isinstance(detail, dict):
            checks_text += f"- {name}: {detail.get('score', '?')}/10 — {detail.get('comment', '')}\n"

    # 逐轮策略调整
    if round_num >= 3:
        temp = 0.8
        system = (
            "你是一名技术公众号编辑。审稿人已经多次审读了你的文章并反复给出了修改建议，"
            "文章仍存在明显问题。这次需要做大幅度修改：可以重写开头、重组结构、"
            "替换所有模板化表达。不要删除原文中有价值的事实信息。"
        )
    elif round_num >= 2:
        temp = 0.7
        system = (
            "你是一名技术公众号编辑。审稿人已经读完了你的文章并给出了修改建议。"
            "这次需要做较大幅度的修改：如果审稿人说开头不够抓人就重写开头，"
            "如果审稿人说结构模板化就重新组织段落和标题。不要删除原文中有价值的事实信息。"
        )
    else:
        temp = 0.5
        system = (
            "你是一名技术公众号编辑。审稿人已经读完了你的文章并给出了修改建议。"
            "请根据审稿建议逐条修改文章，不要敷衍，不要只改几个词。"
            "保持文章风格：技术博客 + 行业观察。"
        )

    user = f"""以下是审稿人对你文章的评价和修改建议，请据此修改文章。

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
        temperature=temp,
    )
    return polish_llm_article(response)


def _create_reviewer_provider():
    """创建审稿专用的 LLM provider。"""
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


# ---------------------------------------------------------------------------
# v2.5 主流程：逐条全链路处理
# ---------------------------------------------------------------------------

def process_one_candidate(
    candidate: Candidate,
    report_date: str,
    used_outputs: set[str],
    llm_provider,
    reviewer,
    max_review_rounds: int = 3,
    review_pass_score: int = 65,
    github_count: int = 0,
) -> dict | None:
    """逐条处理一篇候选文章：抓原文 → AI阅读 → 写作 → 审稿 → 修订 → 保存。

    Returns:
        成功时返回 article_state dict（含 output_file, stage 等），
        跳过/失败时返回 None。
    """
    idx_label = f"[{len(used_outputs) + 1}/{TARGET_ARTICLES}]"

    # ---- Step 1: 抓取原文 ----
    print(f"\n{idx_label} 📥 抓取原文: {candidate.title[:60]}...")
    enrich_candidate(candidate)
    original_text = candidate.body or candidate.snippet or ""

    if len(original_text) < 300:
        print(f"  [SKIP] 原文抓取不足 ({len(original_text)} 字符)，跳过")
        return None

    print(f"  [OK] 原文 {len(original_text)} 字符")

    # ---- Step 2: AI 阅读原文 → summary + insight + 分类 ----
    print(f"  [READ] AI 阅读原文中...")
    category = classify(candidate)
    summary, insight = _generate_ai_summary_and_insight(candidate, category)
    print(f"  [分类] {category}")
    print(f"  [摘要] {summary[:80]}...")

    # ---- Step 3: AI 质量评估 ----
    if llm_provider:
        print(f"  [EVAL] AI 评估质量中...")
        passed, reason = evaluate_quality_with_ai(candidate, llm_provider)
        print(f"  [{'PASS' if passed else 'SKIP'}] {reason[:100]}")
        if not passed:
            return None

    # ---- Step 4: 构建 item 并推断文章类型 ----
    item = {
        "title": normalize_title(candidate.title),
        "source": candidate.source,
        "url": candidate.url,
        "date": candidate.published_at or report_date,
        "category": category,
        "summary": summary,
        "insight": insight,
    }
    article_type = infer_article_type(item)
    item["article_type"] = article_type

    title = article_title(item)
    output_file = unique_output_file(report_date, title, item, used_outputs)
    output_path = ROOT / output_file

    # ---- Step 5: LLM 写作 ----
    print(f"  [WRITE] LLM 生成文章中...")
    try:
        article_text = llm_provider.chat(
            build_llm_writer_prompt(item, original_text),
        )
        article_text = polish_llm_article(article_text)
        from src.common.utils import chinese_char_count as ccc
        print(f"  [DONE] 初稿 {ccc(article_text)} 字")
    except Exception as exc:
        print(f"  [FAIL] 写作失败: {exc}")
        return None

    # ---- Step 6: 审稿修订循环 ----
    review_rounds = 0
    review_score = 0
    review_passed = False
    score_history: list[dict] = []

    if reviewer:
        for round_num in range(1, max_review_rounds + 1):
            print(f"  [REVIEW] 第 {round_num}/{max_review_rounds} 轮审稿...")
            result = review_article(
                article_text=article_text,
                article_type=article_type,
                title=title,
                source_title=candidate.title,
                source_url=candidate.url,
                reviewer=reviewer,
            )

            current_score = result["score"]
            score_history.append({
                "round": round_num,
                "score": current_score,
                "checks": result.get("checks", {}),
                "passed": result["passed"],
            })

            if result["passed"]:
                print(f"  ✅ 审稿通过 (评分: {current_score})")
                review_rounds = round_num
                review_score = current_score
                review_passed = True
                break

            print(f"  ❌ 审稿未通过 (评分: {current_score}, 门槛: {review_pass_score})")
            for s in result.get("suggestions", [])[:3]:
                print(f"    💬 {s}")

            # 退化检测
            if len(score_history) >= 2:
                prev_score = score_history[-2]["score"]
                if current_score <= prev_score:
                    print(f"  ⚠️ 评分从 {prev_score} 降至 {current_score}")
                    if len(score_history) >= 3:
                        two_ago = score_history[-3]["score"]
                        if current_score <= prev_score <= two_ago:
                            print(f"  🛑 连续三轮评分下降，提前终止")
                            review_rounds = round_num
                            review_score = current_score
                            break

            # 已达最大轮数
            if round_num >= max_review_rounds:
                print(f"  ⚠️ 已达最大修订轮数")
                review_rounds = round_num
                review_score = current_score
                break

            # 修改文章
            print(f"  🔄 根据审稿建议修改中...")
            try:
                article_text = revise_article_with_feedback(
                    article_text=article_text,
                    original_text=original_text,
                    item=item,
                    review_result=result,
                    round_num=round_num,
                    llm_provider=llm_provider,
                )
                from src.common.utils import chinese_char_count as ccc
                print(f"  📝 修改完成 ({ccc(article_text)} 字)")
            except Exception as exc:
                print(f"  ❌ 修改失败: {exc}")
                review_rounds = round_num
                review_score = current_score
                break

    # ---- Step 7: 保存文章 ----
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(article_text, encoding="utf-8")
    from src.common.utils import chinese_char_count as ccc
    print(f"  💾 保存: {output_file} ({ccc(article_text)} 字)")

    stage = "review_passed" if review_passed else ("drafted" if not reviewer else "review_failed")
    return {
        "date": report_date,
        "title": candidate.title,
        "url": candidate.url,
        "source": candidate.source,
        "article_type": article_type,
        "category": category,
        "output_file": output_file,
        "stage": stage,
        "char_count": ccc(article_text),
        "review_rounds": review_rounds,
        "review_score": review_score,
        "review_score_history": score_history,
        "summary": summary,
        "insight": insight,
    }


def run_full_pipeline(report_date: str, days: int) -> dict:
    """v2.5 主入口：逐条全链路处理 5 篇文章。

    流程：
    1. 收集所有候选（去重、质量过滤）
    2. 逐条处理：抓原文 → AI阅读 → 质量评估 → 写作 → 审稿 → 修订
    3. 写 report JSON（记录最终状态）
    4. 返回完整报告

    Returns:
        {"date": ..., "items": [...], "article_states": [...], ...}
    """
    import os as _os

    logs: list[FetchLog] = []
    candidates = collect_candidates(days, logs)

    print(f"\n{'='*60}")
    print(f"Pipeline: {report_date} (逐条全链路模式)")
    print(f"共抓取 {len(candidates)} 个候选条目")
    print(f"{'='*60}")

    # 去重 + 质量过滤
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[Candidate] = []
    low_quality_patterns = [
        r"^rss$", r"^feed$", r"\.css", r"\.js$", r"^https?://",
        r"^[a-z-]{1,5}$", r"^\\\"", r"^\d+$",
    ]
    for c in candidates:
        if c.url in seen_urls:
            continue
        if c.title in seen_titles:
            continue
        title_lower = c.title.lower().strip()
        if any(re.search(p, title_lower) for p in low_quality_patterns):
            print(f"  过滤低质量标题: {c.title[:50]}")
            continue
        if len(c.title) < 5 and "github" not in title_lower:
            continue
        seen_urls.add(c.url)
        seen_titles.add(c.title)
        deduped.append(c)
    print(f"去重后剩余 {len(deduped)} 个候选")

    # 选取候选（GitHub 最多 2）
    github_candidates = [c for c in deduped if source_bucket(c.source) == "GitHub"]
    non_github = [c for c in deduped if source_bucket(c.source) != "GitHub"]
    pool: list[Candidate] = []
    pool.extend(github_candidates[:MAX_GITHUB])
    pool.extend(non_github[:TARGET_ARTICLES - len(pool)])
    remaining = [c for c in deduped if c not in pool]
    while len(pool) < TARGET_ARTICLES and remaining:
        pool.append(remaining.pop(0))
    pool = pool[:TARGET_ARTICLES]
    print(f"候选池 {len(pool)} 篇（GitHub: {len([c for c in pool if source_bucket(c.source)=='GitHub'])}）")

    # ---- 逐条处理 ----
    # 初始化 LLM providers
    from src.infrastructure.llm_client import create_llm_provider

    try:
        writer_llm = create_llm_provider()
        print(f"\n[LLM] 写作模型: {writer_llm.model}")
    except Exception as exc:
        print(f"\n[ERROR] 无法创建写作 LLM provider: {exc}")
        raise SystemExit(1)

    reviewer = _create_reviewer_provider()
    if reviewer:
        print(f"[审稿] 审稿模型: {reviewer.model}")
    else:
        print("[审稿] 未配置独立审稿 Agent，将跳过审稿环节")

    # 从 harness.toml 读取审稿配置
    review_max_rounds = 3
    review_pass_score = 65
    try:
        harness_cfg_path = ROOT / "harness.toml"
        if harness_cfg_path.exists():
            import tomllib as _toml
            with harness_cfg_path.open("rb") as f:
                cfg = _toml.load(f)
            review_max_rounds = cfg.get("llm", {}).get("review_max_rounds", 3)
            review_pass_score = cfg.get("article", {}).get("min_score_for_publish", 65)
    except Exception:
        pass

    used_outputs: set[str] = set()
    article_states: list[dict] = []
    github_count = 0
    processed_count = 0

    print(f"\n{'='*60}")
    print(f"开始逐条处理（最多 {review_max_rounds} 轮审稿，通过门槛 {review_pass_score} 分）")
    print(f"{'='*60}")

    for i, candidate in enumerate(pool):
        is_github = source_bucket(candidate.source) == "GitHub"

        if is_github and github_count >= MAX_GITHUB:
            print(f"\n  [SKIP] GitHub 已达上限: {candidate.title[:50]}")
            continue

        result = process_one_candidate(
            candidate=candidate,
            report_date=report_date,
            used_outputs=used_outputs,
            llm_provider=writer_llm,
            reviewer=reviewer,
            max_review_rounds=review_max_rounds,
            review_pass_score=review_pass_score,
            github_count=github_count,
        )

        if result:
            article_states.append(result)
            if is_github:
                github_count += 1
            processed_count += 1
        else:
            article_states.append({
                "date": report_date,
                "title": candidate.title,
                "url": candidate.url,
                "source": candidate.source,
                "stage": "skipped",
            })

        # 中间休息，避免触发限流
        time.sleep(1)

    # ---- 构建最终报告 ----
    items = [
        {
            "title": s.get("title", ""),
            "source": s.get("source", ""),
            "url": s.get("url", ""),
            "date": s.get("date", report_date),
            "category": s.get("category", ""),
            "summary": s.get("summary", ""),
            "insight": s.get("insight", ""),
            "relevance": 4,
            "article_type": s.get("article_type", ""),
            "output_file": s.get("output_file", ""),
            "stage": s.get("stage", ""),
            "review_score": s.get("review_score", 0),
            "review_rounds": s.get("review_rounds", 0),
        }
        for s in article_states
        if s.get("output_file")
    ]

    print(f"\n{'='*60}")
    passed = sum(1 for s in article_states if s.get("stage") == "review_passed")
    drafted = sum(1 for s in article_states if s.get("stage") in ("drafted", "review_failed"))
    skipped = sum(1 for s in article_states if s.get("stage") == "skipped")
    print(f"Pipeline 完成: {passed} 篇审稿通过, {drafted} 篇已生成, {skipped} 篇跳过")
    print(f"产出 {len(items)} 篇文章")
    print(f"{'='*60}")

    return {
        "date": report_date,
        "topic_focus": "前沿 AI 技术动态，聚焦 AI Agent、Harness Engineering、Context Engineering 与多模态大模型。",
        "items": items,
        "article_states": article_states,
        "_fetch_logs": [asdict(log) for log in logs],
    }


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the AI tech daily report (per-article pipeline).")
    parser.add_argument("--date", default=date.today().isoformat(), help="Report date in YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=1, help="Lookback window in days (default 1 = today only).")
    parser.add_argument("--output", default=None, help="Custom output path for report JSON.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing articles.")
    return parser.parse_args()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()

    print(f"[AGENT] 启动 agent for date={args.date}, days={args.days}, overwrite={args.overwrite}")
    sys.stdout.flush()

    # 检查是否已有产出（跳过已存在的文章）
    existing = list(PAPER_DIR.glob(f"{args.date}-*.md"))
    if existing and not args.overwrite:
        print(f"[INFO] 已存在 {len(existing)} 篇当日文章，跳过生成")
        print(f"[INFO] 如需重新生成，请使用 --overwrite 参数")

        output_path = Path(args.output) if args.output else REPORTS_DIR / f"{args.date}.json"

        # 尝试从 article_states 回读元数据（上次运行时的真实数据）
        states_dir = ROOT / "states"
        states_path = states_dir / f"{args.date}.article_states.json"
        states_by_file: dict[str, dict] = {}
        if states_path.exists():
            try:
                states_data = json.loads(states_path.read_text(encoding="utf-8"))
                for s in states_data.get("articles", []):
                    out = s.get("output_file", "")
                    if out:
                        states_by_file[out] = s
            except (json.JSONDecodeError, ValueError):
                pass

        items = []
        for f in sorted(existing):
            rel_path = str(f.relative_to(ROOT)).replace("\\", "/")
            text = f.read_text(encoding="utf-8")
            h1 = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
            title = h1.group(1) if h1 else f.stem

            # 从 article_states 回读真实元数据
            state = states_by_file.get(rel_path, {})
            url = state.get("url", "") or f"file://{rel_path}"
            category = state.get("category", "") or "AI Agent"
            items.append({
                "title": title,
                "source": state.get("source", "") or "已存在",
                "url": url,
                "date": state.get("date", "") or args.date,
                "category": category,
                "summary": state.get("summary", "") or title,
                "insight": state.get("insight", "") or INSIGHTS.get(category, ""),
                "relevance": 3,
                "output_file": rel_path,
                "stage": "kept_existing",
            })
        report = {
            "date": args.date,
            "topic_focus": "前沿 AI 技术动态，聚焦 AI Agent、Harness Engineering、Context Engineering 与多模态大模型。",
            "items": items,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Report written: {output_path} ({len(items)} items)")
        return

    # 运行全链路
    report = run_full_pipeline(args.date, args.days)

    # 写入 report JSON
    output_path = Path(args.output) if args.output else REPORTS_DIR / f"{args.date}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # 只保留 items 和元数据，去掉 article_states（太大了）
    clean_report = {k: v for k, v in report.items() if k != "article_states"}
    output_path.write_text(json.dumps(clean_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport written: {output_path} ({len(report['items'])} items)")

    # 写入 article_states
    states_dir = ROOT / "states"
    states_dir.mkdir(parents=True, exist_ok=True)
    states_path = states_dir / f"{args.date}.article_states.json"
    states_path.write_text(
        json.dumps({
            "date": args.date,
            "articles": report.get("article_states", []),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"States written: {states_path}")


if __name__ == "__main__":
    main()
