"""src/infrastructure/browser_fetcher.py - 基于 Selenium + Chrome 的浏览器抓取模块。

用于绕过搜狗等网站的反爬机制，抓取 JS 动态加载的内容。
支持：
- 搜狗微信搜索结果抓取
- 搜狗跳转链接解析（获取真实 mp.weixin.qq.com URL）
- 微信文章全文抓取
- 通用网页抓取（fallback）
"""

from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
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


# ---------------------------------------------------------------------------
# Chrome 浏览器管理
# ---------------------------------------------------------------------------

def _create_chrome_options(headless: bool = True) -> Options:
    """创建 Chrome 浏览器选项。"""
    options = Options()
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
        driver = webdriver.Chrome(options=options)
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

        # 检测反爬页面
        if _is_antispider(page_source):
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
        driver.get(sogou_url)
        time.sleep(2)
        current_url = driver.current_url
        if "mp.weixin.qq.com" in current_url:
            return current_url
    except Exception:
        pass
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
    """检测是否为反爬页面。"""
    lowered = html_text.lower()
    return any(
        token in lowered
        for token in ("antispider", "verify", "imgcode", "captcha")
    )


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
    from datetime import datetime

    # 方式1: timeConvert 时间戳
    block_start = page_source.find(block)
    surrounding = page_source[max(0, block_start - 500):block_start + 5000]
    ts_match = re.search(r"timeConvert\(\s*['\"]?(\d{10})", surrounding)
    if ts_match:
        try:
            return datetime.fromtimestamp(int(ts_match.group(1))).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            pass

    # 方式2: 中文日期格式
    combined = _clean_html(block)
    date_match = re.search(
        r"(\d{4})[\u5e74\-\/](\d{1,2})[\u6708\-\/](\d{1,2})[\u65e5]?",
        combined,
    )
    if date_match:
        return f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"

    # 方式3: 纯数字日期
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", combined)
    if date_match:
        return date_match.group(1)

    return ""


# ---------------------------------------------------------------------------
# 便捷函数：一站式抓取
# ---------------------------------------------------------------------------

def fetch_wechat_source_full(
    account_name: str,
    query: str = "AI",
    max_articles: int = 5,
    headless: bool = True,
) -> list[dict]:
    """一站式抓取微信公众号来源的文章（含搜索 + URL 解析 + 内容抓取）。

    在同一个浏览器会话中完成所有操作，保持 cookie/session 连续性。

    Args:
        account_name: 公众号名称
        query: 搜索关键词
        max_articles: 最多抓取几篇
        headless: 是否无头模式

    Returns:
        dict 列表，每个包含: title, source, url(真实URL), published_at, snippet, body(全文)
    """
    articles: list[dict] = []
    path_backup = _sanitize_path()
    driver = None

    try:
        options = _create_chrome_options(headless=headless)
        driver = webdriver.Chrome(options=options)
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
        # Step 1: 搜索搜狗微信
        # 搜狗已不支持直接 URL 带 query 参数，必须先访问首页再通过搜索框提交
        search_query = f"{account_name} {query}"
        try:
            # 先访问搜狗微信首页
            driver.get("https://weixin.sogou.com/")
            time.sleep(3)
            # 找到搜索框并输入关键词
            search_input = driver.find_element(By.ID, "query")
            search_input.clear()
            search_input.send_keys(search_query)
            # 点击"搜文章"提交按钮
            search_btn = driver.find_element(By.CSS_SELECTOR, "input[type='submit']")
            search_btn.click()
            time.sleep(4)
            # 等待搜索结果出现
            try:
                WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "li[id^='sogou_vr_']"))
                )
            except TimeoutException:
                time.sleep(3)
        except Exception as exc:
            print(f"[browser_fetcher] 搜索框提交失败: {exc}，尝试直接 URL")
            # 回退：尝试直接 URL
            try:
                driver.get(f"https://weixin.sogou.com/weixin?type=2&query={search_query}&tsn=1")
                time.sleep(5)
            except TimeoutException:
                pass

        page_source = driver.page_source

        if _is_antispider(page_source):
            print(f"[browser_fetcher] 搜狗返回验证码页，跳过")
            return articles

        # 解析搜索结果块
        blocks = re.findall(
            r'(<li id="sogou_vr_11002601_box_\d+".*?</li>)',
            page_source, flags=re.S,
        )

        if not blocks:
            # 尝试更宽松的匹配
            blocks = re.findall(
                r'<li[^>]*id="sogou_vr_11002601_box_\d+"[^>]*>.*?</li>',
                page_source, flags=re.S,
            )

        if not blocks:
            print(f"[browser_fetcher] 搜狗搜索无结果 (query={search_query[:40]}), page_len={len(page_source)})")

        for block in blocks:
            if len(articles) >= max_articles:
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
            published_at = _extract_publish_time(page_source, block)

            # Step 2: 在同一 session 中解析真实微信 URL
            try:
                driver.get(sogou_url)
                time.sleep(2)
                real_url = driver.current_url
            except Exception:
                continue

            if "mp.weixin.qq.com" not in real_url:
                continue

            # Step 3: 在同一 session 中抓取文章全文
            time.sleep(1.5)
            body = _fetch_article_body(driver)

            if not body or len(body) < 100:
                continue

            articles.append({
                "title": title,
                "source": f"{account_name}（微信公众号）",
                "url": real_url,
                "sogou_url": sogou_url,
                "published_at": published_at,
                "snippet": snippet,
                "body": body,
            })

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
            try:
                text = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                text = _clean_html(page_source)

        # 移除噪声
        text = re.sub(r'微信扫一扫[^\n]*', '', text)
        text = re.sub(r'关注该公众号[^\n]*', '', text)
        text = re.sub(r'微信号[：:][^\n]*', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text
    except Exception:
        return ""
