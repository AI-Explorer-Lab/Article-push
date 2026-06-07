"""src/infrastructure/browser_fetcher.py - 微信正文抓取与遗留浏览器工具。

当前 pipeline 的微信公众号 URL 发现已改由
`wechat_foreground_collector.py` 通过 Mac 微信前台短接管完成。本模块仍负责
微信文章正文抓取，并保留旧 Selenium/搜狗工具以便手动诊断或后续回滚参考。

支持：
- 微信文章全文抓取
- 通用网页抓取（fallback）
- 遗留搜狗微信搜索结果抓取
- 遗留搜狗跳转链接解析（获取真实 mp.weixin.qq.com URL）
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Generator
from urllib.parse import quote_plus
from urllib.request import Request, build_opener, ProxyHandler

from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

WECHAT_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "MicroMessenger/8.0.50 NetType/WIFI Language/zh_CN"
)

PAGE_LOAD_TIMEOUT = 25  # 秒
IMPLICIT_WAIT = 5       # 秒


@dataclass
class SogouSearchResult:
    """搜狗微信搜索结果。"""
    title: str
    url: str           # 搜狗跳转链接 (https://weixin.sogou.com/link?url=...)
    snippet: str = ""
    published_at: str = ""
    real_url: str = ""  # 真实微信文章 URL (mp.weixin.qq.com/s/...)


# ---------------------------------------------------------------------------
# ChromeDriver 路径修复
# ---------------------------------------------------------------------------

def _sanitize_path() -> dict[str, str]:
    """修复 PATH 环境变量，避免旧版 chromedriver 干扰 Selenium Manager。

    Selenium 4.x 内置 Selenium Manager 会自动下载匹配的 ChromeDriver，
    但 PATH 中旧版本会被优先使用。此函数临时移除已知的旧版路径。
    """
    original_path = os.environ.get("PATH", "")
    filtered = [
        p for p in original_path.split(os.pathsep)
        if "Anaconda" not in p or "Scripts" not in p
    ]
    os.environ["PATH"] = os.pathsep.join(filtered)
    return {"original_path": original_path}


def _restore_path(original_path: str) -> None:
    """恢复原始 PATH。"""
    os.environ["PATH"] = original_path


def _find_existing_chromedriver() -> str | None:
    """优先复用本机已有 chromedriver，避免 Selenium Manager 联网下载。"""
    from pathlib import Path
    from shutil import which

    found = which("chromedriver")
    if found:
        return found

    cache_root = Path.home() / ".cache" / "selenium" / "chromedriver"
    if not cache_root.exists():
        return None

    candidates = sorted(
        (p for p in cache_root.rglob("chromedriver") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return str(candidates[0]) if candidates else None


def _create_chrome_driver(options: Options) -> webdriver.Chrome:
    """创建 Chrome driver，优先使用本地缓存 driver。"""
    driver_path = _find_existing_chromedriver()
    if driver_path:
        return webdriver.Chrome(service=ChromeService(driver_path), options=options)
    return webdriver.Chrome(options=options)


# ---------------------------------------------------------------------------
# Chrome 浏览器管理
# ---------------------------------------------------------------------------

def _create_chrome_options(headless: bool = True) -> Options:
    """创建 Chrome 浏览器选项。"""
    options = Options()
    options.page_load_strategy = "eager"
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"--user-agent={USER_AGENT}")
    # 禁用自动化检测标志
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    # 额外反检测参数
    options.add_argument("--disable-gpu")
    options.add_argument("--lang=zh-CN")
    options.add_argument("--accept-lang=zh-CN,zh;q=0.9")
    prefs = {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }
    options.add_experimental_option("prefs", prefs)
    return options


@contextmanager
def create_browser(headless: bool = True) -> Generator[webdriver.Chrome, None, None]:
    """创建并管理 Chrome 浏览器实例（上下文管理器）。

    用法:
        with create_browser() as driver:
            driver.get("https://example.com")
            html = driver.page_source
    """
    path_backup = _sanitize_path()
    driver = None
    try:
        options = _create_chrome_options(headless=headless)
        driver = _create_chrome_driver(options)
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        driver.implicitly_wait(IMPLICIT_WAIT)
        # 注入反检测脚本，隐藏自动化特征
        driver.execute_script("""
            // 移除 navigator.webdriver 标志
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            // 伪造 chrome.runtime
            window.chrome = { runtime: {} };
            // 伪造 plugins 和 languages
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
        """)
        yield driver
    finally:
        _restore_path(path_backup["original_path"])
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 搜狗微信搜索
# ---------------------------------------------------------------------------

def fetch_sogou_wechat_with_browser(
    account_name: str,
    query: str,
    max_results: int = 5,
    headless: bool = True,
) -> list[SogouSearchResult]:
    """使用 Selenium 浏览器抓取搜狗微信搜索结果。

    相比纯 urllib 方式，此方法可以：
    - 绕过搜狗的反爬虫验证页面
    - 获取 JS 动态加载的完整搜索结果
    - 提取时间戳等动态数据

    Args:
        account_name: 公众号名称
        query: 搜索关键词
        max_results: 最大结果数
        headless: 是否无头模式

    Returns:
        SogouSearchResult 列表
    """
    results: list[SogouSearchResult] = []
    search_query = f"{account_name} {query}"
    search_url = f"https://weixin.sogou.com/weixin?type=2&query={search_query}"

    with create_browser(headless=headless) as driver:
        try:
            driver.get(search_url)
            time.sleep(2)  # 等待 JS 渲染
        except TimeoutException:
            pass  # 页面可能已部分加载

        page_source = driver.page_source

        # 检测反爬页面（Selenium 有反检测措施，通常不会被拦截，但如果被拦截则直接返回）
        if _is_antispider(page_source):
            print("[browser_fetcher] 搜狗触发反爬验证，跳过")
            return results

        # 解析搜索结果块
        blocks = re.findall(
            r'(<li id="sogou_vr_11002601_box_\d+".*?</li>)',
            page_source, flags=re.S,
        )

        for block in blocks:
            if len(results) >= max_results:
                break

            title_match = re.search(
                r'id="sogou_vr_11002601_title_\d+"[^>]*>(.*?)</a>',
                block, re.S,
            )
            href_match = re.search(r'href="(/link\?url=[^"]+)"', block, re.S)
            summary_match = re.search(
                r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.S,
            )

            if not title_match or not href_match:
                continue

            title = _clean_html(title_match.group(1))[:80]
            sogou_url = "https://weixin.sogou.com" + href_match.group(1)
            snippet = _clean_html(summary_match.group(1))[:200] if summary_match else ""

            # 提取发布时间
            published_at = _extract_publish_time(page_source, block)

            results.append(SogouSearchResult(
                title=title,
                url=sogou_url,
                snippet=snippet,
                published_at=published_at,
            ))

    return results


def resolve_wechat_url(driver: webdriver.Chrome, sogou_url: str) -> str:
    """通过搜狗跳转链接获取真实微信文章 URL。

    访问搜狗的 /link?url=... 链接，会重定向到真实的 mp.weixin.qq.com 页面。
    支持两种微信 URL 格式：
    - /s/文章ID?chksm=... （标准格式）
    - /s?src=11&timestamp=...&signature=... （搜狗跳转格式）

    Args:
        driver: Chrome 浏览器实例
        sogou_url: 搜狗跳转链接

    Returns:
        真实的微信文章 URL，失败时返回空字符串
    """
    try:
        driver.get(_make_sogou_link_clickable(sogou_url))
        return _wait_for_wechat_url(driver)
    except Exception:
        pass
    return ""


def _wait_for_wechat_url(driver: webdriver.Chrome, timeout: float = 8.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            current_url = driver.current_url
        except Exception:
            current_url = ""
        if "mp.weixin.qq.com" in current_url:
            return current_url
        time.sleep(0.4)
    return ""


def _resolve_wechat_url_with_retries(
    driver: webdriver.Chrome,
    sogou_url: str,
    retries: int = 3,
) -> str:
    """解析搜狗跳转，失败时重新生成 k/h 参数再试。"""
    for attempt in range(1, retries + 1):
        try:
            _set_user_agent(driver, WECHAT_USER_AGENT)
            driver.get(_make_sogou_link_clickable(sogou_url))
            current_url = _wait_for_wechat_url(driver)
            if current_url:
                return current_url
            print(
                f"  [browser_fetcher]   跳转未到微信(第{attempt}次): "
                f"{driver.current_url[:120]}"
            )
        except TimeoutException:
            print(f"  [browser_fetcher]   搜狗跳转超时(第{attempt}次)")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        except WebDriverException as exc:
            print(f"  [browser_fetcher]   搜狗跳转失败(第{attempt}次): {str(exc)[:120]}")
    return ""


def _resolve_wechat_url_by_click(driver: webdriver.Chrome, meta: dict) -> str:
    """回到搜狗结果页真实点击标题链接，保留 referer 和搜狗点击校验。"""
    search_page_url = meta.get("search_page_url", "")
    title_id = meta.get("title_id", "")
    if not search_page_url or not title_id:
        return ""

    try:
        _set_user_agent(driver, USER_AGENT)
        driver.get(search_page_url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, title_id))
        )
        original_handles = set(driver.window_handles)
        link = driver.find_element(By.ID, title_id)
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", link)
        time.sleep(0.5)
        _set_user_agent(driver, WECHAT_USER_AGENT)
        link.click()

        new_handles = [h for h in driver.window_handles if h not in original_handles]
        if new_handles:
            driver.switch_to.window(new_handles[-1])

        current_url = _wait_for_wechat_url(driver)
        if current_url:
            return current_url
    except Exception as exc:
        print(f"  [browser_fetcher]   点击搜狗结果失败: {str(exc)[:120]}")

    return ""


def fetch_wechat_article(
    driver: webdriver.Chrome,
    article_url: str,
) -> str:
    """抓取微信文章全文。

    支持两种微信 URL 格式：
    - /s/文章ID （标准格式）
    - /s?src=11&timestamp=...&signature=... （搜狗跳转格式）

    Args:
        driver: Chrome 浏览器实例
        article_url: 微信文章 URL (mp.weixin.qq.com/s/...)

    Returns:
        清洗后的文章文本
    """
    try:
        driver.get(article_url)
        time.sleep(2.5)  # 微信页面 JS 渲染较慢

        # 优先：直接从 #js_content 元素提取文本
        try:
            js_content = driver.find_element(By.ID, "js_content")
            text = js_content.text
            if text and len(text) >= 100:
                text = re.sub(r'\s+', ' ', text).strip()
                return text
        except Exception:
            pass

        # 回退：正则提取
        page_source = driver.page_source
        content_match = re.search(
            r'<div[^>]*id="js_content"[^>]*>(.*?)</div>',
            page_source, re.S,
        )
        if content_match:
            text = _clean_html(content_match.group(1))
        else:
            try:
                text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                text = _clean_html(page_source)

        # 移除常见噪声
        text = re.sub(r'微信扫一扫[^\n]*', '', text)
        text = re.sub(r'关注该公众号[^\n]*', '', text)
        text = re.sub(r'微信号[：:][^\n]*', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except Exception:
        return ""


def fetch_wechat_articles_batch(
    results: list[SogouSearchResult],
    max_articles: int = 5,
    headless: bool = True,
) -> list[SogouSearchResult]:
    """批量解析搜狗搜索结果，获取真实微信文章 URL 并抓取内容。

    在同一个浏览器会话中完成所有操作，减少开销。

    Args:
        results: 搜狗搜索结果列表
        max_articles: 最多抓取几篇文章
        headless: 是否无头模式

    Returns:
        更新了 real_url 的搜索结果列表（去除了无法解析的项）
    """
    valid: list[SogouSearchResult] = []
    with create_browser(headless=headless) as driver:
        for result in results:
            if len(valid) >= max_articles:
                break
            # 解析真实 URL
            real_url = resolve_wechat_url(driver, result.url)
            if real_url:
                result.real_url = real_url
                valid.append(result)
    return valid


def fetch_generic_page(url: str, headless: bool = True) -> str:
    """使用浏览器抓取任意网页内容（作为 urllib 的 fallback）。

    Args:
        url: 目标 URL
        headless: 是否无头模式

    Returns:
        页面 HTML 文本
    """
    with create_browser(headless=headless) as driver:
        try:
            driver.get(url)
            time.sleep(2)
            return driver.page_source
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _is_antispider(html_text: str) -> bool:
    """检测是否为搜狗反爬拦截页面。

    注意：不检测 erweima 模板代码（搜狗正常页面也包含隐藏的二维码弹窗模板），
    只检测真正的拦截页面特征。
    """
    lowered = html_text.lower()

    # 真正的拦截页面特征
    if any(token in lowered for token in ("antispider", "verifycode", "imgcode")):
        return True

    # captcha 关键词 + 拦截页面特有结构
    if "captcha" in lowered and ("验证码" in lowered or "verify" in lowered):
        return True

    return False


def _clean_html(html_text: str) -> str:
    """清洗 HTML 文本。"""
    import html as _html
    text = _html.unescape(html_text or "")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_publish_time(page_source: str, block: str) -> str:
    """从搜狗搜索结果中提取发布时间。"""
    combined = _clean_html(block)

    # 方式1: 结果块中已经渲染出的可见日期。优先读它，避免串到前一条
    # 结果附近的 timeConvert 脚本。
    date_match = re.search(
        r"(\d{4})[\u5e74\-\/.](\d{1,2})[\u6708\-\/.](\d{1,2})[\u65e5]?",
        combined,
    )
    if date_match:
        return (
            f"{date_match.group(1)}-"
            f"{int(date_match.group(2)):02d}-"
            f"{int(date_match.group(3)):02d}"
        )

    relative_days = None
    if re.search(r"\d+\s*(?:分钟|小时)前", combined):
        relative_days = 0
    elif "今天" in combined:
        relative_days = 0
    elif "昨天" in combined:
        relative_days = 1
    elif "前天" in combined:
        relative_days = 2
    else:
        day_match = re.search(r"(\d+)\s*天前", combined)
        if day_match:
            relative_days = int(day_match.group(1))
    if relative_days is not None:
        return (date.today() - timedelta(days=relative_days)).strftime("%Y-%m-%d")

    # 方式2: 当前结果块内的 timeConvert 时间戳
    ts_match = re.search(r"timeConvert\(\s*['\"]?(\d{10})", block)
    if ts_match:
        try:
            return datetime.fromtimestamp(int(ts_match.group(1))).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            pass

    # 方式3: 结果块后方紧邻的 timeConvert 时间戳。只向后看，不向前看，
    # 否则第一页第二条会读到第一条的日期。
    block_start = page_source.find(block)
    if block_start >= 0:
        surrounding = page_source[block_start:block_start + len(block) + 1500]
        ts_match = re.search(r"timeConvert\(\s*['\"]?(\d{10})", surrounding)
        if ts_match:
            try:
                return datetime.fromtimestamp(int(ts_match.group(1))).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                pass

    # 方式4: 纯数字日期
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", combined)
    if date_match:
        return date_match.group(1)

    return ""


def _parse_target_date(value: str | date | None) -> date | None:
    """解析 CLI/report 传入的目标日期。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _build_date_window(target_date: str | date | None, days: int) -> tuple[date, date] | None:
    """返回闭区间 [start, end]，days=1 表示只抓 target_date 当天。"""
    end_date = _parse_target_date(target_date)
    if not end_date:
        return None
    lookback_days = max(1, days)
    return end_date - timedelta(days=lookback_days - 1), end_date


def _is_in_date_window(published_at: str, window: tuple[date, date] | None) -> bool:
    if not window:
        return True
    if not published_at:
        return False
    try:
        pub_date = datetime.strptime(published_at, "%Y-%m-%d").date()
    except ValueError:
        return False
    start_date, end_date = window
    return start_date <= pub_date <= end_date


def _normalize_account_name(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def _is_target_account(source_account: str, account_name: str) -> bool:
    source = _normalize_account_name(source_account)
    target = _normalize_account_name(account_name)
    return bool(source and target and source == target)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = _normalize_account_name(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(value.strip())
    return deduped


def _get_wechat_account_id(account_name: str) -> str:
    try:
        from src.constants.wechat_sources import WECHAT_ACCOUNT_IDS
    except Exception:
        return ""
    return str(WECHAT_ACCOUNT_IDS.get(account_name, "") or "").strip()


def _get_source_account_names(account_name: str) -> list[str]:
    """返回搜狗结果中允许匹配的公众号来源名。"""
    configured: list[str] = []
    try:
        from src.constants.wechat_sources import WECHAT_SOURCE_ACCOUNTS
        raw_value = WECHAT_SOURCE_ACCOUNTS.get(account_name, [])
        if isinstance(raw_value, str):
            configured = [raw_value]
        else:
            configured = [str(value) for value in raw_value]
    except Exception:
        configured = []
    return _dedupe_keep_order([account_name, *configured])


def _build_search_query(account_name: str, query: str) -> str:
    """构造搜狗搜索词，优先把微信号纳入检索以减少同名误命中。"""
    account_id = _get_wechat_account_id(account_name)
    parts = [account_name]
    if account_id:
        parts.append(account_id)
    if query:
        parts.append(query)
    return " ".join(part for part in parts if part).strip()


def _matches_source_account(meta: dict, account_names: list[str]) -> bool:
    source_account = meta.get("source_account", "")
    return any(_is_target_account(source_account, name) for name in account_names)


def _build_sogou_search_urls(search_query: str, window: tuple[date, date] | None) -> list[tuple[str, bool]]:
    """构造搜狗微信搜索 URL。

    有目标日期时优先尝试搜狗原生日期参数。当前搜狗微信入口经常会把
    tsn/ft/et 请求重定向回首页；这种情况应显式失败，而不是自动退化成
    多页粗筛。若确实需要粗筛兜底，可设置 WECHAT_SOGOU_ALLOW_BROAD_FALLBACK=1。
    """
    encoded_query = quote_plus(search_query)
    base = f"https://weixin.sogou.com/weixin?type=2&query={encoded_query}&ie=utf8"
    urls: list[tuple[str, bool]] = []
    if window:
        start_date, end_date = window
        if start_date == end_date == date.today():
            urls.append((f"{base}&tsn=1", True))
        urls.append((
            f"{base}&tsn=5&ft={start_date.isoformat()}&et={end_date.isoformat()}",
            True,
        ))
        if os.getenv("WECHAT_SOGOU_ALLOW_BROAD_FALLBACK", "").strip() == "1":
            urls.append((base, False))
    else:
        urls.append((base, False))
    return urls


def _get_sogou_max_pages(window: tuple[date, date] | None, used_date_filter: bool) -> int:
    """返回普通搜索兜底时最多查看几页。"""
    if used_date_filter:
        return 1
    default_pages = 1 if window else 5
    raw_value = os.getenv("WECHAT_SOGOU_MAX_PAGES", "").strip()
    if not raw_value:
        return default_pages
    try:
        return max(1, int(raw_value))
    except ValueError:
        return default_pages


def _has_search_results(page_source: str) -> bool:
    return "sogou_vr_" in page_source and "news-list" in page_source


def _is_browser_error_page(page_source: str) -> bool:
    markers = (
        "ERR_CONNECTION_",
        "ERR_TIMED_OUT",
        "ERR_NAME_NOT_RESOLVED",
        "This site can",
        "无法访问此网站",
        "网页无法打开",
    )
    return any(marker in (page_source or "") for marker in markers)


def _make_sogou_link_clickable(href: str) -> str:
    """模拟搜狗结果链接被点击时追加的 k/h 参数。

    搜狗结果页的 JS 会在鼠标点击 / mousedown 时给 /link?url=... 追加
    &k=<1..100>&h=<校验字符>。直接 driver.get(原始 href) 时没有这两个参数，
    部分链接不会跳到 mp.weixin.qq.com。
    """
    import html as _html

    url = _html.unescape(href or "")
    if url.startswith("/"):
        url = "https://weixin.sogou.com" + url

    if "/link?" not in url or "&k=" in url:
        return url

    url_pos = url.find("url=")
    if url_pos < 0:
        return url

    k = random.randint(1, 100)
    h_pos = url_pos + 4 + 21 + k
    if h_pos >= len(url):
        return url

    return f"{url}&k={k}&h={url[h_pos]}"


def _set_user_agent(driver: webdriver.Chrome, user_agent: str) -> None:
    try:
        driver.execute_cdp_cmd("Network.setUserAgentOverride", {"userAgent": user_agent})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 便捷函数：一站式抓取
# ---------------------------------------------------------------------------

def fetch_wechat_source_full(
    account_name: str,
    query: str = "AI",
    max_articles: int = 5,
    headless: bool = True,
    days: int = 7,
    target_date: str | date | None = None,
) -> list[dict]:
    """一站式抓取微信公众号来源的文章（含搜索 + URL 解析 + 内容抓取）。

    在同一个浏览器会话中完成所有操作，保持 cookie/session 连续性。

    Args:
        account_name: 公众号名称
        query: 搜索关键词
        max_articles: 最多抓取几篇
        headless: 是否无头模式
        days: 时间回溯天数，days=1 表示只抓 target_date 当天
        target_date: 目标日期（YYYY-MM-DD）；为空时默认今天

    Returns:
        dict 列表，每个包含: title, source, url(真实URL), published_at, snippet, body(全文)
    """
    articles: list[dict] = []
    path_backup = _sanitize_path()
    driver = None

    try:
        print(f"[browser_fetcher] 启动 Chrome 浏览器（headless={headless}）...")
        sys.stdout.flush()
        options = _create_chrome_options(headless=headless)
        # 优先使用 Selenium 4.x 内置的 Selenium Manager 自动管理 ChromeDriver，
        # 如果失败则尝试 webdriver_manager 作为回退
        try:
            driver = _create_chrome_driver(options)
        except Exception:
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                service = ChromeService(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=options)
            except ImportError:
                raise
        print(f"[browser_fetcher] Chrome 启动成功")
        sys.stdout.flush()
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        driver.implicitly_wait(IMPLICIT_WAIT)
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
    except Exception as exc:
        print(f"[browser_fetcher] Failed to create Chrome driver: {exc}")
        _restore_path(path_backup["original_path"])
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        return articles

    try:
        # Step 1: 直接打开普通搜狗搜索页，然后在本地按发布时间精确过滤。
        # 搜狗日期筛选参数在自动化会话里会频繁返回空页或关闭连接。
        search_query = _build_search_query(account_name, query)
        source_account_names = _get_source_account_names(account_name)
        date_window = _build_date_window(target_date or date.today(), days)

        print(f"[browser_fetcher] 搜索: {search_query[:40]}")
        print(f"[browser_fetcher] 来源账号匹配: {', '.join(source_account_names)}")
        if date_window:
            print(
                f"[browser_fetcher] 日期窗口: {date_window[0].isoformat()} ~ "
                f"{date_window[1].isoformat()}"
            )
        sys.stdout.flush()

        search_urls = _build_sogou_search_urls(search_query, date_window)
        attempted_date_filter = any(is_date_filter for _url, is_date_filter in search_urls)
        allow_broad_fallback = any(not is_date_filter for _url, is_date_filter in search_urls)
        used_date_filter = False
        try:
            print(f"[browser_fetcher] 访问搜狗搜索页面...")
            sys.stdout.flush()
            _set_user_agent(driver, USER_AGENT)
            loaded = False
            for search_url, is_date_filter in search_urls:
                page_source = ""
                attempts = 1 if is_date_filter else 3
                if is_date_filter:
                    print(f"[browser_fetcher] 尝试搜狗日期筛选 URL: {search_url[:120]}")
                for attempt in range(1, attempts + 1):
                    try:
                        driver.get(search_url)
                    except TimeoutException as exc:
                        print(f"[browser_fetcher] 搜索 URL 超时: {exc.msg[:120]}")
                        try:
                            driver.execute_script("window.stop();")
                        except Exception:
                            pass
                        try:
                            page_source = driver.page_source
                        except WebDriverException:
                            page_source = ""
                    except WebDriverException as exc:
                        print(
                            f"[browser_fetcher] 搜索 URL 访问失败"
                            f"(第{attempt}/{attempts}次): {str(exc)[:120]}"
                        )
                        page_source = ""

                    if not page_source:
                        time.sleep(5)  # 等待 JS 渲染搜索结果

                        try:
                            WebDriverWait(driver, 10).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, "li[id^='sogou_vr_']"))
                            )
                        except TimeoutException:
                            time.sleep(2)

                        try:
                            page_source = driver.page_source
                        except WebDriverException as exc:
                            print(f"[browser_fetcher] 读取搜索页失败: {str(exc)[:120]}")
                            page_source = ""

                    if (
                        page_source
                        and not _is_browser_error_page(page_source)
                        and (_has_search_results(page_source) or attempt == attempts)
                    ):
                        break

                    if attempt < attempts:
                        print("[browser_fetcher] 搜索页未返回有效结果，稍后重试")
                        try:
                            driver.get("about:blank")
                        except Exception:
                            pass
                        time.sleep(2 * attempt)

                if len(page_source) >= 10000 and _has_search_results(page_source):
                    used_date_filter = is_date_filter
                    if used_date_filter:
                        print("[browser_fetcher] 搜狗日期筛选 URL 生效，只收集当前结果页")
                    loaded = True
                    break

                if is_date_filter:
                    print(
                        f"[browser_fetcher] 搜狗日期筛选 URL 不可用"
                        f"(长度={len(page_source)})"
                    )
                    try:
                        driver.get("about:blank")
                    except Exception:
                        pass
                    time.sleep(1)

            # 如果所有直接 URL 方式都失败，只有在明确允许粗筛时才回退到
            # 首页搜索框。目标日期模式默认要求精确筛选失败就停止。
            if not loaded and allow_broad_fallback:
                print("[browser_fetcher] 直接URL被重定向或无结果，改用首页搜索框方式")
                for attempt in range(1, 4):
                    try:
                        driver.get("https://weixin.sogou.com/")
                        time.sleep(3)
                        search_input = driver.find_element(By.ID, "query")
                        search_input.clear()
                        search_input.send_keys(search_query)
                        search_btn = driver.find_element(By.CSS_SELECTOR, "input[type='submit']")
                        search_btn.click()
                        time.sleep(5)
                        try:
                            WebDriverWait(driver, 10).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, "li[id^='sogou_vr_']"))
                            )
                        except TimeoutException:
                            time.sleep(3)
                        if _has_search_results(driver.page_source):
                            loaded = True
                            break
                    except Exception as exc:
                        print(
                            f"[browser_fetcher] 首页搜索框方式失败"
                            f"(第{attempt}/3次): {str(exc)[:120]}"
                        )
                        time.sleep(2 * attempt)
            elif not loaded and attempted_date_filter:
                print("[browser_fetcher] 搜狗原生日期筛选不可用，停止本账号搜狗抓取")
                return articles
        except Exception as exc:
            print(f"[browser_fetcher] 搜索失败: {exc}")
            try:
                driver.get("https://weixin.sogou.com/")
                time.sleep(3)
            except TimeoutException:
                pass

        # Step 2: 分两阶段处理 ——
        # 阶段 A: 收集搜索结果的链接和元数据（不抓正文，避免 navigate 破坏结果页状态）
        # 阶段 B: 统一逐篇抓取正文

        page_num = 0
        # 搜狗文章搜索默认不是严格按发布时间倒序。日期 URL 生效时只看
        # 服务端筛选后的当前页；普通搜索兜底时可用 WECHAT_SOGOU_MAX_PAGES
        # 控制最多收集页数。
        max_pages = _get_sogou_max_pages(date_window, used_date_filter)
        if date_window and not used_date_filter and not os.getenv("WECHAT_SOGOU_MAX_PAGES"):
            if attempted_date_filter:
                print(
                    "[browser_fetcher] 搜狗日期筛选 URL 未生效；如需普通搜索粗筛，"
                    "可设置 WECHAT_SOGOU_ALLOW_BROAD_FALLBACK=1"
                )
            else:
                print(
                    "[browser_fetcher] 搜狗微信当前未启用可用 URL 时间筛选；"
                    "如需扩大范围可设置 WECHAT_SOGOU_MAX_PAGES"
                )
        print(f"[browser_fetcher] 阶段A: 收集候选链接 (最多查看 {max_pages} 页)")

        # 阶段 A: 收集链接
        candidate_metas: list[dict] = []  # {title, sogou_url, snippet, published_at}
        try:
            initial_source = driver.page_source
        except WebDriverException:
            initial_source = ""
        if not _has_search_results(initial_source):
            print("[browser_fetcher] 未能打开有效搜狗结果页，停止本账号抓取")
            return articles

        while page_num < max_pages:
            page_num += 1
            search_page_url = driver.current_url
            page_source = driver.page_source

            if _is_antispider(page_source):
                print(f"[browser_fetcher] 搜狗触发反爬验证（第{page_num}页），停止收集")
                break

            blocks = re.findall(
                r'(<li id="sogou_vr_11002601_box_\d+".*?</li>)',
                page_source, flags=re.S,
            )
            if not blocks:
                blocks = re.findall(
                    r'<li[^>]*id="sogou_vr_11002601_box_\d+"[^>]*>.*?</li>',
                    page_source, flags=re.S,
                )

            if not blocks:
                li_ids = re.findall(r'<li[^>]*id="([^"]*)"', page_source)
                sample_ids = li_ids[:10] if li_ids else ['(无li标签)']
                print(f"[browser_fetcher] 搜狗第{page_num}页无结果 (query={search_query[:40]}, page_len={len(page_source)}, li_ids={sample_ids})")
                break

            print(f"[browser_fetcher] 搜狗第{page_num}页: {len(blocks)} 条结果")

            for block in blocks:
                title_match = re.search(
                    r'id="(sogou_vr_11002601_title_\d+)"[^>]*>(.*?)</a>',
                    block, re.S,
                )
                href_match = re.search(r'href="(/link\?url=[^"]+)"', block, re.S)
                summary_match = re.search(
                    r'<p class="txt-info"[^>]*>(.*?)</p>', block, re.S,
                )
                account_match = re.search(
                    r'<div class="s-p"[^>]*>.*?<span class="all-time-y2"[^>]*>(.*?)</span>',
                    block, re.S,
                )
                if not title_match or not href_match:
                    continue

                published_at = _extract_publish_time(page_source, block)
                sogou_url = "https://weixin.sogou.com" + href_match.group(1)
                candidate_metas.append({
                    "title": _clean_html(title_match.group(2))[:80],
                    "sogou_url": sogou_url,
                    "search_page_url": search_page_url,
                    "title_id": title_match.group(1),
                    "snippet": _clean_html(summary_match.group(1))[:200] if summary_match else "",
                    "published_at": published_at,
                    "source_account": _clean_html(account_match.group(1)) if account_match else "",
                })

            if page_num >= max_pages:
                break

            # 查看下一页（仍在搜索结果页，不会 navigate 到文章页）
            page_turned = False
            try:
                next_btn = driver.find_element(By.ID, "sogou_next")
                if next_btn and next_btn.is_displayed():
                    next_btn.click()
                    page_turned = True
            except Exception:
                pass

            if not page_turned:
                try:
                    page_turned = driver.execute_script("""
                        var next = document.getElementById('sogou_next');
                        if (next && next.offsetParent !== null) { next.click(); return true; }
                        var links = document.querySelectorAll('a');
                        for (var i = 0; i < links.length; i++) {
                            if (links[i].textContent.indexOf('下一页') >= 0 && links[i].offsetParent !== null) {
                                links[i].click(); return true;
                            }
                        }
                        return false;
                    """)
                except Exception:
                    pass

            if page_turned:
                time.sleep(3)
            else:
                print(f"[browser_fetcher] 第{page_num}页无下一页，停止收集")
                break

        print(f"[browser_fetcher] 阶段A完成: 共收集 {len(candidate_metas)} 个候选链接 (查看{page_num}页)")

        if date_window:
            before_count = len(candidate_metas)
            candidate_metas = [
                meta for meta in candidate_metas
                if _is_in_date_window(meta.get("published_at", ""), date_window)
            ]
            print(
                f"[browser_fetcher] 本地日期过滤: {before_count} → {len(candidate_metas)} "
                f"({date_window[0].isoformat()}~{date_window[1].isoformat()})"
            )

        exact_account_metas = [
            meta for meta in candidate_metas
            if _matches_source_account(meta, source_account_names)
        ]
        if exact_account_metas:
            if len(exact_account_metas) != len(candidate_metas):
                print(
                    f"[browser_fetcher] 公众号精确过滤: {len(candidate_metas)} → "
                    f"{len(exact_account_metas)} ({', '.join(source_account_names)})"
                )
            candidate_metas = exact_account_metas
        else:
            print(
                f"[browser_fetcher] 未找到来源账号精确匹配 "
                f"{', '.join(source_account_names)} 的结果，丢弃日期命中但来源不符的候选"
            )
            candidate_metas = []

        # 搜狗默认按相关性排序，不是按时间倒序。将候选按发布日期倒序排列，
        # 确保阶段 B 优先抓取最新文章，避免新文章因排在后面而被 max_articles 截断。
        candidate_metas.sort(
            key=lambda m: m.get("published_at", ""),
            reverse=True,
        )

        # 阶段 B: 逐篇抓取正文
        print(f"[browser_fetcher] 阶段B: 逐篇抓取正文 (最多 {max_articles} 篇)...")
        sys.stdout.flush()
        for meta in candidate_metas:
            if len(articles) >= max_articles:
                break

            _set_user_agent(driver, USER_AGENT)
            print(
                f"  [browser_fetcher]   → 解析: {meta['published_at']} "
                f"{meta.get('source_account', '')} / {meta['title'][:40]}"
            )
            real_url = _resolve_wechat_url_by_click(driver, meta)
            if not real_url:
                real_url = _resolve_wechat_url_with_retries(driver, meta["sogou_url"])

            if "mp.weixin.qq.com" not in real_url:
                print(f"  [browser_fetcher]   跳过: 未解析到微信 URL")
                continue

            time.sleep(1.5)
            body = _fetch_article_body(driver)

            if not body or len(body) < 100:
                preview = (body or "")[:120]
                page_title = ""
                try:
                    page_title = driver.title
                except Exception:
                    pass
                print(
                    f"  [browser_fetcher]   跳过: 正文过短 "
                    f"({len(body) if body else 0} 字符) "
                    f"title={page_title!r} url={driver.current_url[:120]} {preview!r}"
                )
                continue

            articles.append({
                "title": meta["title"],
                "source": f"{account_name}（微信公众号）",
                "url": real_url,
                "sogou_url": meta["sogou_url"],
                "published_at": meta["published_at"],
                "snippet": meta["snippet"],
                "body": body,
            })
            print(f"  [browser_fetcher]   ✓ {meta['title'][:40]} ({len(body)} 字符)")
            sys.stdout.flush()

    except Exception as exc:
        print(f"[browser_fetcher] Error during wechat fetch: {exc}")
    finally:
        _restore_path(path_backup["original_path"])
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return articles


def _fetch_article_body(driver: webdriver.Chrome) -> str:
    """从微信文章页面提取正文（内部辅助函数）。"""
    try:
        page_source = driver.page_source
        page_text = ""
        try:
            page_text = driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            page_text = _clean_html(page_source)

        if "HTTP ERROR 403" in page_text or "请求遭到拒绝" in page_text or "未获授权" in page_text:
            return _fetch_wechat_body_direct(driver.current_url)

        # 优先：直接从 #js_content 元素提取文本
        try:
            js_content = driver.find_element(By.ID, "js_content")
            text = js_content.text
            if text and len(text) >= 100:
                text = re.sub(r'\s+', ' ', text).strip()
                return text
        except Exception:
            pass

        # 回退：用正则提取 js_content 内容
        content_match = re.search(
            r'<div[^>]*id="js_content"[^>]*>(.*?)</div>',
            page_source, re.S,
        )
        if content_match:
            text = _clean_html(content_match.group(1))
        else:
            # 最后回退：body 文本
            text = page_text

        # 移除噪声
        text = re.sub(r'微信扫一扫[^\n]*', '', text)
        text = re.sub(r'关注该公众号[^\n]*', '', text)
        text = re.sub(r'微信号[：:][^\n]*', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) < 100 and "mp.weixin.qq.com" in driver.current_url:
            return _fetch_wechat_body_direct(driver.current_url)
        return text
    except Exception:
        return ""


def _fetch_wechat_body_direct(article_url: str) -> str:
    """直连抓微信正文。

    macOS 系统代理可能把 mp.weixin.qq.com 发到海外出口，腾讯会直接返回
    403。这里显式禁用 urllib 代理，只对已由浏览器解析出的微信正文 URL
    做直连读取，再从 #js_content 提取原文。
    """
    if "mp.weixin.qq.com" not in (article_url or ""):
        return ""

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://weixin.sogou.com/",
    }
    try:
        opener = build_opener(ProxyHandler({}))
        response = opener.open(Request(article_url, headers=headers), timeout=30)
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        html_text = raw.decode(charset, errors="ignore")
    except Exception as exc:
        print(f"  [browser_fetcher]   直连抓微信正文失败: {str(exc)[:120]}")
        return ""

    return _extract_wechat_body_from_html(html_text)


def _extract_wechat_body_from_html(html_text: str) -> str:
    """从微信文章 HTML 中提取 #js_content 原文文本。"""
    if not html_text:
        return ""
    from html.parser import HTMLParser

    class WeChatContentParser(HTMLParser):
        void_tags = {"br", "img", "input", "meta", "link", "hr", "area", "base", "col", "embed", "param", "source", "track", "wbr"}

        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.depth = 0
            self.skip_depth = 0
            self.parts: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attr_map = {key: value for key, value in attrs}
            if self.depth == 0 and attr_map.get("id") == "js_content":
                self.depth = 1
                return
            if self.depth > 0:
                if tag in {"script", "style"}:
                    self.skip_depth += 1
                if tag == "br":
                    self.parts.append("\n")
                if tag not in self.void_tags:
                    self.depth += 1

        def handle_endtag(self, tag: str) -> None:
            if self.depth <= 0:
                return
            if tag in {"script", "style"} and self.skip_depth > 0:
                self.skip_depth -= 1
            if tag not in self.void_tags:
                self.depth -= 1

        def handle_data(self, data: str) -> None:
            if self.depth > 0 and self.skip_depth == 0:
                self.parts.append(data)

    parser = WeChatContentParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass

    text = "".join(parser.parts)
    if not text:
        content_match = re.search(
            r'<div[^>]*id=["\']js_content["\'][^>]*>(.*?)</div>',
            html_text,
            re.S | re.I,
        )
        text = _clean_html(content_match.group(1)) if content_match else _clean_html(html_text)

    text = re.sub(r'微信扫一扫[^\n]*', '', text)
    text = re.sub(r'关注该公众号[^\n]*', '', text)
    text = re.sub(r'微信号[：:][^\n]*', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text
