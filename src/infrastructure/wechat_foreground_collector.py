"""Foreground WeChat URL collector.

This module drives the visible desktop WeChat client for the narrow part that
cannot be done reliably through Sogou/Chrome: finding same-day articles on a
public-account homepage and copying their mp.weixin.qq.com URLs.

It intentionally keeps the foreground-control surface small. The caller should
hand URLs to the normal background article fetcher after collection.
"""

from __future__ import annotations

import os
import json
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
except Exception:
    pass


TMP_DIR = Path(os.getenv("TEMP") or os.getenv("TMP") or "/private/tmp") if sys.platform == "win32" else Path("/private/tmp")
DEBUG_DIR = TMP_DIR / "wechat_foreground_debug"
_WIN_VISUAL_MAXIMIZE_ATTEMPTED = False
_WIN_WINDOW_TITLE_HINT = ""
_WIN_WINDOW_PROCESS_HINT = ""


@dataclass
class OCRText:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass
class WeChatArticleURL:
    title: str
    url: str
    published_at: str
    snippet: str = ""


@dataclass
class WindowsWindowInfo:
    handle: int
    left: int
    top: int
    width: int
    height: int


class WeChatForegroundError(RuntimeError):
    """Raised when foreground WeChat collection cannot continue safely."""


def collect_wechat_article_urls(
    account_name: str,
    max_articles: int = 10,
    days: int = 1,
    target_date: str | date | None = None,
    prompt_user: bool = True,
) -> list[WeChatArticleURL]:
    """Collect article URLs from the visible WeChat client on this platform."""
    if sys.platform == "darwin":
        return _collect_wechat_article_urls_macos(
            account_name=account_name,
            max_articles=max_articles,
            days=days,
            target_date=target_date,
            prompt_user=prompt_user,
        )
    if sys.platform == "win32":
        return _collect_wechat_article_urls_windows(
            account_name=account_name,
            max_articles=max_articles,
            days=days,
            target_date=target_date,
            prompt_user=prompt_user,
        )
    raise WeChatForegroundError(f"foreground WeChat collection does not support {sys.platform}")


def _collect_wechat_article_urls_macos(
    account_name: str,
    max_articles: int = 10,
    days: int = 1,
    target_date: str | date | None = None,
    prompt_user: bool = True,
) -> list[WeChatArticleURL]:
    """Collect article URLs from the visible Mac WeChat client.

    Preconditions:
    - macOS with WeChat running and logged in.
    - The user allows a short foreground-control window.
    - The WeChat main window is visible or can be activated.

    The current implementation optimizes for the verified workflow:
    search account -> open account homepage -> click articles under "today" ->
    copy address bar URL.
    """
    target = _coerce_target_date(target_date)
    date_labels = _date_labels_for_window(target, days)

    if prompt_user and _is_interactive():
        print(
            "\n[wechat_foreground] 即将接管微信前台 20-60 秒。"
            "请确保微信已登录，并暂时不要操作鼠标键盘。"
        )
        input("[wechat_foreground] 准备好后按 Enter 继续；按 Ctrl+C 取消...")

    _activate_wechat()
    _standardize_wechat_windows()
    _sleep(0.5, 0.15)
    _dismiss_storage_warning()
    _raise_if_wechat_locked()
    _ensure_main_search_visible()

    search_obs = _find_top_left_search_placeholder()
    if not search_obs:
        _debug_capture(account_name, "missing_search")
        _print_visible_texts(account_name, "missing_search")
        raise WeChatForegroundError(
            "未找到微信左上搜索框。请把微信主窗口/聊天列表切到前台后重试。"
        )

    print(f"[wechat_foreground] 搜索公众号: {account_name}", flush=True)
    _click_norm(search_obs.cx, search_obs.cy)
    _sleep(0.3, 0.1)
    _paste_text(account_name)
    _sleep(0.9, 0.15)
    _dismiss_storage_warning()
    _debug_capture(account_name, "after_search")

    compact_account = _find_account_result(account_name)
    if not compact_account:
        _debug_capture(account_name, "missing_followed_account_result")
        _print_visible_texts(account_name, "missing_followed_account_result")
        raise WeChatForegroundError(f"未找到已关注公众号搜索结果: {account_name}")

    print(f"[wechat_foreground] 进入公众号主页: {account_name}", flush=True)
    _click_norm(compact_account.cx, compact_account.cy)
    _sleep(0.25, 0.05)
    _click_norm(compact_account.cx, compact_account.cy)
    _sleep(1.2, 0.15)
    _standardize_wechat_windows(account_name)
    _debug_capture(account_name, "compact_account_click")

    if not _wait_for_account_page(timeout=4.0):
        _debug_capture(account_name, "compact_not_account_page")
        print(f"[wechat_foreground] 小弹层未进入主页，改用搜一搜账号筛选: {account_name}", flush=True)
        _open_account_page_from_soso(account_name)
        _sleep(1.2, 0.15)
        _standardize_wechat_windows(account_name)
        if not _wait_for_account_page(timeout=4.0):
            _debug_capture(account_name, "not_account_page")
            _print_visible_texts(account_name, "not_account_page")
            raise WeChatForegroundError(f"点击后未进入公众号主页: {account_name}")

    _ensure_full_account_home(account_name)
    _debug_capture(account_name, "account_page")

    urls: list[WeChatArticleURL] = []
    visited: set[str] = set()
    seen_urls: set[str] = set()

    try:
        for _attempt in range(max_articles * 3):
            if len(urls) >= max_articles:
                break

            article = _find_next_article_on_account_page(date_labels, visited_titles=visited)
            if not article:
                _debug_capture(account_name, "missing_article")
                _print_visible_texts(account_name, "missing_article")
                print(
                    f"[wechat_foreground] 未找到日期分组 {date_labels} 下的文章: "
                    f"{account_name}",
                    flush=True,
                )
                break

            print(f"[wechat_foreground] 打开文章: {article.text}", flush=True)
            visited.add(article.text)
            _open_article_card(article)
            _debug_capture(account_name, "article_page")

            url = _copy_visible_article_url()
            if "mp.weixin.qq.com" in url and url not in seen_urls:
                print(f"[wechat_foreground] 获取 URL: {url[:90]}", flush=True)
                published_date, date_ok = _verified_wechat_article_date(url, target, days)
                if not date_ok:
                    label = published_date.isoformat() if published_date else "unknown"
                    print(
                        f"[wechat_foreground] 跳过非目标日期文章: {label} {url[:90]}",
                        flush=True,
                    )
                    seen_urls.add(url)
                    _return_to_account_page()
                    _ensure_full_account_home(account_name)
                    _sleep(1.2, 0.25)
                    continue
                seen_urls.add(url)
                urls.append(
                    WeChatArticleURL(
                        title=article.text,
                        url=url,
                        published_at=(published_date or target).isoformat(),
                        snippet=f"微信前台采集: {account_name}",
                    )
                )
            elif url in seen_urls:
                print(f"[wechat_foreground] 跳过重复 URL: {url[:90]}", flush=True)
            else:
                print(f"[wechat_foreground] 未复制到微信文章 URL: {url[:120]}", flush=True)

            _return_to_account_page()
            _ensure_full_account_home(account_name)
            _sleep(1.2, 0.25)
    finally:
        _cleanup_after_account()

    if prompt_user:
        print("[wechat_foreground] 本轮微信前台接管结束，可以继续使用电脑。", flush=True)

    return urls


def _sleep(base: float, jitter: float = 0.35) -> None:
    """Small random wait for UI rendering stability."""
    time.sleep(max(0.05, base + random.uniform(-jitter, jitter)))


def _coerce_target_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
    return date.today()


def _date_labels_for_window(target: date, days: int) -> list[str]:
    """Return labels likely shown by WeChat for the desired date window."""
    today = date.today()
    labels: list[str] = []
    if target == today:
        labels.append("今天")
    elif (today - target).days == 1:
        labels.append("昨天")
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    labels.append(weekdays[target.weekday()])
    labels.append(f"{target.year}年{target.month}月{target.day}日")
    labels.append(target.strftime("%Y-%m-%d"))
    if days > 1:
        labels.extend(["今天", "昨天"])
    return list(dict.fromkeys(labels))


def _date_window_for_target(target: date, days: int) -> tuple[date, date]:
    lookback_days = max(1, days)
    return target - timedelta(days=lookback_days - 1), target


def _date_in_window(value: date, target: date, days: int) -> bool:
    start, end = _date_window_for_target(target, days)
    return start <= value <= end


def _fetch_wechat_article_published_date(url: str, timeout: float = 8.0) -> date | None:
    """Best-effort extraction of a WeChat article publish date from its HTML."""
    if "mp.weixin.qq.com" not in url:
        return None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        request = Request(url, headers=headers)
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html_text = response.read(700_000).decode(charset, errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return None
    return _extract_wechat_article_published_date_from_html(html_text)


def _extract_wechat_article_published_date_from_html(html_text: str) -> date | None:
    text = html_text or ""
    timestamp_patterns = (
        r"\bct\s*=\s*['\"](\d{10})['\"]",
        r"\bcreateTime\s*=\s*['\"](\d{10})['\"]",
        r"\boriCreateTime\s*=\s*['\"](\d{10})['\"]",
        r'"createTime"\s*:\s*"?(\d{10})',
        r'"oriCreateTime"\s*:\s*"?(\d{10})',
        r'"ct"\s*:\s*"?(\d{10})',
    )
    for pattern in timestamp_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            return datetime.fromtimestamp(int(match.group(1))).date()
        except (ValueError, OSError):
            continue

    date_patterns = (
        r'id=["\']publish_time["\'][^>]*>\s*([^<]+)',
        r'\bpublish_time\s*=\s*["\']([^"\']+)',
        r'(\d{4})[\u5e74\-/\.](\d{1,2})[\u6708\-/\.](\d{1,2})\s*[\u65e5]?',
    )
    for pattern in date_patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        candidate = match.group(1)
        if match.lastindex and match.lastindex >= 3:
            candidate = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
        parsed = _parse_loose_date(candidate)
        if parsed:
            return parsed
    return None


def _parse_loose_date(value: str) -> date | None:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    match = re.search(r"(\d{4})[\u5e74\-/\.](\d{1,2})[\u6708\-/\.](\d{1,2})", cleaned)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _verified_wechat_article_date(url: str, target: date, days: int) -> tuple[date | None, bool]:
    published = _fetch_wechat_article_published_date(url)
    if published is None:
        if os.getenv("WECHAT_FOREGROUND_REQUIRE_DATE_VERIFY", "1").strip() == "0":
            return None, True
        return None, False
    return published, _date_in_window(published, target, days)


def _is_interactive() -> bool:
    return sys.stdin.isatty() and os.getenv("WECHAT_FOREGROUND_ASSUME_READY") != "1"


def _run(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WeChatForegroundError(f"命令超时: {' '.join(cmd[:3])}") from exc
    if result.returncode != 0:
        raise WeChatForegroundError((result.stderr or result.stdout or str(cmd)).strip())
    return result.stdout


def _activate_wechat() -> None:
    _run(["open", "-a", "WeChat"], timeout=5.0)
    _run([
        "osascript",
        "-e",
        'tell application "WeChat" to activate',
        "-e",
        'tell application "System Events" to set visible of process "WeChat" to true',
        "-e",
        'tell application "System Events" to set frontmost of process "WeChat" to true',
    ])


def _main_screen_visible_bounds() -> tuple[int, int, int, int]:
    """Return the main screen's usable bounds in AppleScript top-left coords."""
    swift = r"""
import AppKit
let screen = NSScreen.main!
let frame = screen.frame
let visible = screen.visibleFrame
let x = Int(visible.origin.x)
let y = Int(frame.height - visible.origin.y - visible.height)
let w = Int(visible.width)
let h = Int(visible.height)
print("\(x)\t\(y)\t\(w)\t\(h)")
"""
    out = _run(["swift", "-e", swift], timeout=20.0).strip()
    x, y, w, h = out.split("\t")
    return int(x), int(y), int(w), int(h)


def _standardize_wechat_windows(title_hint: str = "") -> None:
    """Maximize one WeChat window without entering macOS native fullscreen."""
    try:
        x, y, w, h = _main_screen_visible_bounds()
    except Exception:
        x, y, w, h = 0, 24, 1440, 850

    escaped_hint = title_hint.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "System Events"
  if exists process "WeChat" then
    tell process "WeChat"
      set visible to true
      set frontmost to true
      set targetWindow to missing value
      repeat with win in windows
        try
          if "{escaped_hint}" is not "" and (name of win as text) contains "{escaped_hint}" then
            set targetWindow to win
            exit repeat
          end if
        end try
      end repeat
      if targetWindow is missing value and exists window 1 then
        set targetWindow to window 1
      end if
      if targetWindow is not missing value then
        try
          perform action "AXRaise" of targetWindow
          set position of targetWindow to {{{x}, {y}}}
          set size of targetWindow to {{{w}, {h}}}
        end try
      end if
    end tell
  end if
end tell
'''
    try:
        _run(["osascript", "-e", script], timeout=6.0)
    except WeChatForegroundError:
        # Window resizing is a stability optimization. If AX refuses one window,
        # keep the collector moving with OCR-based coordinates.
        return


def _screenshot_path() -> Path:
    path = TMP_DIR / f"wechat_foreground_{int(time.time() * 1000)}.png"
    _run(["screencapture", "-x", str(path)], timeout=5.0)
    for _ in range(10):
        if path.exists() and path.stat().st_size > 0:
            break
        time.sleep(0.05)
    return path


def _screen_info(image_path: Path) -> tuple[int, int, float, float]:
    swift = """
import AppKit
import Foundation
let img = NSImage(contentsOfFile: CommandLine.arguments[1])!
let rep = img.representations[0]
let frame = NSScreen.main!.frame
print("\\(rep.pixelsWide)\\t\\(rep.pixelsHigh)\\t\\(frame.width)\\t\\(frame.height)")
"""
    out = _run(["swift", "-e", swift, str(image_path)], timeout=20.0).strip()
    pw, ph, sw, sh = out.split("\t")
    return int(pw), int(ph), float(sw), float(sh)


def _ocr(image_path: Path | None = None) -> list[OCRText]:
    path = image_path or _screenshot_path()
    swift = r"""
import Foundation
import Vision
import AppKit
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let img = NSImage(contentsOf: url),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    exit(0)
}
let req = VNRecognizeTextRequest { req, err in
    let obs = (req.results as? [VNRecognizedTextObservation]) ?? []
    for o in obs {
        if let t = o.topCandidates(1).first {
            let bb = o.boundingBox
            print("\(bb.origin.x)\t\(bb.origin.y)\t\(bb.size.width)\t\(bb.size.height)\t\(t.string)")
        }
    }
}
req.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
req.recognitionLevel = .accurate
req.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
"""
    out = _run(["swift", "-e", swift, str(path)], timeout=20.0)
    texts: list[OCRText] = []
    for line in out.splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        try:
            texts.append(
                OCRText(
                    text=parts[4].strip(),
                    x=float(parts[0]),
                    y=float(parts[1]),
                    w=float(parts[2]),
                    h=float(parts[3]),
                )
            )
        except ValueError:
            continue
    return texts


def _click_norm(norm_x: float, norm_y_from_bottom: float) -> None:
    image_path = _screenshot_path()
    pixel_w, pixel_h, screen_w, screen_h = _screen_info(image_path)
    x_px = norm_x * pixel_w
    y_px_from_top = (1.0 - norm_y_from_bottom) * pixel_h
    x = x_px / (pixel_w / screen_w)
    y = y_px_from_top / (pixel_h / screen_h)
    _click_point(x, y)


def _click_point(x: float, y: float) -> None:
    swift = f"""
import CoreGraphics
import Foundation
let p = CGPoint(x: {x:.1f}, y: {y:.1f})
let src = CGEventSource(stateID: .hidSystemState)
CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
usleep(80000)
CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
"""
    _run(["swift", "-e", swift], timeout=20.0)


def _open_article_card(article: OCRText) -> None:
    """Open a public-account article by clicking stable points on its card."""
    _click_norm(article.cx, article.cy)
    _sleep(2.0, 0.2)
    _dismiss_storage_warning()


def _paste_text(text: str) -> None:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    _run([
        "osascript",
        "-e",
        f'set the clipboard to "{escaped}"',
        "-e",
        'tell application "System Events" to keystroke "a" using command down',
        "-e",
        'tell application "System Events" to keystroke "v" using command down',
    ])


def _copy_visible_article_url() -> str:
    _raise_if_wechat_locked()
    print("[wechat_foreground] 复制链接: 打开文章菜单", flush=True)
    menu_url = _copy_article_url_from_more_menu()
    _raise_if_wechat_locked()
    url = _extract_mp_weixin_url(menu_url)
    if url:
        return url
    return ""


def _extract_mp_weixin_url(text: str) -> str:
    match = re.search(r"https?://mp\.weixin\.qq\.com/[\w?%&=./:#~+\-]+", text)
    return match.group(0) if match else ""


def _wait_for_article_page(title: str, timeout: float = 15.0) -> None:
    """Wait until the WeChat article page shows real content, not a loading tab."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        texts = _ocr()
        visible = " | ".join(item.text for item in texts)
        if "Mac 微信已被锁定" in visible:
            raise WeChatForegroundError("Mac 微信已被锁定，请先在手机微信顶部状态栏解锁")
        account_home_markers = (
            "已关注",
            "发消息",
            "篇原创内容",
            "个朋友关注",
            "置顶",
            "全部 | 贴图 | 文章",
        )
        is_account_home = any(marker in visible for marker in account_home_markers)
        is_article_page = (
            "微信公众平台" in visible
            or "原创" in visible
            or "写留言" in visible
            or "听全文" in visible
            or "赞作者" in visible
            or "阅读原文" in visible
        )
        if "正在加载" not in visible and is_article_page and not is_account_home:
            return
        _sleep(1.0, 0.1)
    _debug_capture("article_page", "wait_timeout")
    raise WeChatForegroundError(f"文章页未在 {timeout:.0f} 秒内打开: {title}")


def _wait_for_account_page(timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _looks_like_account_page():
            return True
        _sleep(0.5, 0.08)
    return False


def _copy_article_url_from_more_menu() -> str:
    """Use the article window's top-right menu to copy the article link."""
    click_points = []
    more_button = _find_article_more_button()
    if more_button:
        click_points.append((more_button.cx, more_button.cy))
    click_points.extend([(0.979, 0.942), (0.965, 0.942), (0.648, 0.040)])

    for x, y in click_points:
        _click_norm(x, y)
        _sleep(0.7, 0.12)
        _debug_capture("article_menu", "opened")
        print("[wechat_foreground] 复制链接: 菜单已点击，查找复制链接", flush=True)

        copy_item = (
            _find_menu_text_containing("复制链接")
            or _find_menu_text_containing("拷贝链接")
            or _find_menu_text_containing("复制 link")
        )
        if copy_item:
            print(f"[wechat_foreground] 复制链接: 点击 {copy_item}", flush=True)
            _click_norm(copy_item.cx, copy_item.cy)
            _sleep(1.0, 0.15)
            return _clipboard().strip()

    print("[wechat_foreground] 复制链接: 菜单中未识别到复制链接", flush=True)
    return ""


def _find_article_more_button() -> OCRText | None:
    texts = _ocr()
    candidates = [
        item for item in texts
        if any(mark in item.text for mark in ("••", "...", "⋯"))
        and 0.55 < item.x < 0.99
        and 0.86 < item.y < 0.95
    ]
    if candidates:
        return sorted(candidates, key=lambda item: (-item.y, item.x))[0]

    visible = " | ".join(item.text for item in texts)
    if "微信公众平台" in visible or "写留言" in visible or "听全文" in visible:
        # OCR often misses the tiny article-window "..." button. In the current
        # Mac WeChat layout it sits at the far top-right of the article window.
        return OCRText("article_more_fallback", 0.955, 0.925, 0.045, 0.035)
    return None


def _clipboard() -> str:
    return _run(["pbpaste"], timeout=3.0)


def _go_back() -> None:
    _run(["osascript", "-e", 'tell application "System Events" to key code 123 using command down'])


def _press_enter() -> None:
    _run(["osascript", "-e", 'tell application "System Events" to key code 36'])


def _close_current_window() -> None:
    _run(["osascript", "-e", 'tell application "System Events" to keystroke "w" using command down'])


def _ensure_main_search_visible() -> None:
    """Return to the WeChat main window where the left-top search field exists."""
    for attempt in range(5):
        _raise_if_wechat_locked()
        _dismiss_storage_warning()
        if _looks_like_account_page():
            _close_current_window()
            _sleep(0.8, 0.2)
            continue
        if _looks_like_collection_page():
            _open_wechat_chats_tab()
            _sleep(0.8, 0.2)
            continue
        if _looks_like_soso_page():
            _close_current_window()
            _sleep(0.8, 0.2)
            continue
        if _find_top_left_search_placeholder():
            return
        if attempt == 0:
            _activate_wechat()
        _close_current_window()
        _sleep(0.8, 0.2)


def _return_to_account_page() -> None:
    """Leave an article page and return to the public-account homepage."""
    for attempt in range(6):
        if _looks_like_soso_page():
            _close_current_window()
            _sleep(0.9, 0.2)
            continue
        if _looks_like_account_page():
            return
        if attempt == 0:
            # Prefer closing the current article tab/window. Some article pages
            # are opened beside an old Soso tab, and browser Back returns to the
            # Soso tab instead of the account home behind the window.
            _close_current_window()
        elif attempt == 1:
            _close_current_window()
        else:
            _go_back()
        _sleep(0.9, 0.25)
    if not _looks_like_account_page():
        raise WeChatForegroundError("未能从文章页返回公众号主页")


def _cleanup_after_account() -> None:
    """Close account/article web windows so the next account starts from main search."""
    if os.getenv("WECHAT_FOREGROUND_KEEP_ACCOUNT_WINDOW", "").strip() == "1":
        return
    for _ in range(5):
        if _looks_like_wechat_locked():
            return
        _dismiss_storage_warning()
        if _looks_like_account_page():
            _close_current_window()
            _sleep(0.8, 0.2)
            continue
        if _looks_like_collection_page():
            _open_wechat_chats_tab()
            _sleep(0.8, 0.2)
            continue
        if _looks_like_soso_page():
            _close_current_window()
            _sleep(0.8, 0.2)
            continue
        if _find_top_left_search_placeholder():
            return
        _close_current_window()
        _sleep(0.8, 0.2)


def _looks_like_account_page() -> bool:
    texts = _ocr()
    visible = " | ".join(item.text for item in texts)
    if _visible_has_soso_markers(visible):
        return False
    has_account_tab = any(item.text in {"全部", "贴图", "文章", "视频号"} for item in texts)
    menu_labels = {
        "投稿合作", "专栏报告", "AI大会", "比赛信息", "课程学习", "联系我们",
        "联系我", "大模型", "技术写作",
    }
    has_bottom_menu = sum(
        1 for item in texts
        if item.y < 0.22 and any(label in item.text for label in menu_labels)
    ) >= 2
    has_profile = any(
        "已关注" in item.text
        or "发消息" in item.text
        or "篇原创内容" in item.text
        or any(label in item.text for label in menu_labels)
        for item in texts
    )
    return (has_account_tab and has_profile) or has_bottom_menu


def _looks_like_full_account_home() -> bool:
    texts = _ocr()
    visible = " | ".join(item.text for item in texts)
    if _visible_has_soso_markers(visible):
        return False
    if "微信公众平台" in visible or "写留言" in visible or "听全文" in visible:
        return False
    tab_count = sum(1 for item in texts if item.text.strip() in {"全部", "贴图", "文章", "视频号"})
    has_profile = any(
        "已关注" in item.text
        or "发消息" in item.text
        or "篇原创内容" in item.text
        or "个朋友关注" in item.text
        for item in texts
    )
    return tab_count >= 2 and has_profile


def _ensure_full_account_home(account_name: str) -> None:
    """Ensure the current public-account view is the full home/history page."""
    if _looks_like_full_account_home():
        return
    _open_full_account_home_from_feed(account_name)
    deadline = time.time() + 4.5
    while time.time() < deadline:
        if _looks_like_full_account_home():
            return
        _sleep(0.45, 0.08)
    _debug_capture(account_name, "missing_full_account_home")
    _print_visible_texts(account_name, "missing_full_account_home")
    raise WeChatForegroundError(f"未能进入公众号完整主页: {account_name}")


def _open_full_account_home_from_feed(account_name: str) -> None:
    """Open the account home/profile from the public-account push feed."""
    if _looks_like_full_account_home():
        return
    if not _looks_like_account_page():
        return

    print(f"[wechat_foreground] 进入公众号完整主页: {account_name}", flush=True)
    # In the public-account feed window, the person icon in the upper-right
    # opens the full account home/history page that shows all same-day articles.
    for x, y in ((0.710, 0.835), (0.945, 0.895), (0.920, 0.835)):
        _click_norm(x, y)
        _sleep(1.5, 0.2)
        _debug_capture(account_name, "full_account_home_click")
        if _looks_like_full_account_home():
            return


def _dismiss_storage_warning() -> None:
    texts = _ocr()
    visible = " | ".join(item.text for item in texts)
    if "存储空间已满" in visible and "无法继续使用微信" in visible:
        raise WeChatForegroundError("微信提示存储空间已满，无法继续使用微信；请先清理微信存储空间")
    for label in ("今日不再提醒", "知道了", "我知道了", "确定"):
        button = next((item for item in texts if label in item.text), None)
        if not button:
            continue
        _click_norm(button.cx, button.cy)
        _sleep(0.4, 0.1)
        return


def _looks_like_wechat_locked() -> bool:
    texts = _ocr()
    return any("Mac 微信已被锁定" in item.text for item in texts)


def _raise_if_wechat_locked() -> None:
    if _looks_like_wechat_locked():
        raise WeChatForegroundError("Mac 微信已被锁定，请先在手机微信顶部状态栏解锁")


def _looks_like_collection_page() -> bool:
    texts = _ocr()
    has_collection = any("全部收藏" in item.text for item in texts)
    has_collection_sidebar = any(
        item.text in {"新建笔记", "搜索范围：", "图片与视频", "聊天记录"}
        for item in texts
    )
    return has_collection and has_collection_sidebar


def _open_wechat_chats_tab() -> None:
    # The chat tab is the upper bubble icon in WeChat's fixed left rail.
    _click_norm(0.126, 0.735)


def _open_wechat_public_account_tab() -> None:
    # In the standardized WeChat window, this left-rail icon opens the selected
    # followed public account page much more reliably than the compact popup.
    _click_norm(0.023, 0.645)


def _looks_like_target_account_page(account_name: str) -> bool:
    texts = _ocr()
    visible = " | ".join(item.text for item in texts)
    return _looks_like_account_page() and account_name.lower() in visible.lower()


def _debug_capture(account_name: str, stage: str) -> None:
    if os.getenv("WECHAT_FOREGROUND_DEBUG", "1").strip() == "0":
        return
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in account_name)
    path = DEBUG_DIR / f"{int(time.time() * 1000)}_{safe_name}_{stage}.png"
    try:
        _run(["screencapture", "-x", str(path)], timeout=5.0)
    except Exception:
        return


def _print_visible_texts(account_name: str, stage: str, limit: int = 24) -> None:
    try:
        texts = sorted(_ocr(), key=lambda item: (-item.y, item.x))
    except Exception as exc:
        print(f"[wechat_foreground] OCR 诊断失败 ({account_name}/{stage}): {exc}", flush=True)
        return
    preview = " | ".join(item.text for item in texts[:limit] if item.text)
    print(f"[wechat_foreground] OCR 诊断 ({account_name}/{stage}): {preview}", flush=True)


def _find_top_left_search_placeholder() -> OCRText | None:
    texts = _ocr()
    candidates = [
        item for item in texts
        if "搜索" in item.text and item.x < 0.35 and item.y > 0.80
    ]
    return sorted(candidates, key=lambda item: (item.x, -item.y))[0] if candidates else None


def _open_soso_search_page(account_name: str) -> None:
    """Open WeChat's full Soso page from the main-window search field."""
    network_result = _find_search_popup_network_result(account_name)
    if network_result:
        _click_norm(network_result.cx, network_result.cy)
        _sleep(2.2, 0.35)
        if _looks_like_soso_page():
            return

    _press_enter()
    _sleep(2.2, 0.35)
    if _looks_like_soso_page():
        return

    # Some builds keep focus in the compact result popup. The query text at the
    # top is the stable entry point to the full Soso page.
    query = _find_search_popup_query(account_name)
    if query:
        _click_norm(query.cx, query.cy)
        _sleep(2.2, 0.35)
        if _looks_like_soso_page():
            return

    more = _find_text_exact("查看更多") or _find_text_containing("查看全部")
    if more:
        _click_norm(more.cx, more.cy)
        _sleep(2.2, 0.35)


def _open_account_page_from_soso(account_name: str) -> None:
    if _looks_like_account_page():
        return
    _ensure_main_search_visible()
    if _looks_like_account_page():
        return
    search_obs = _find_top_left_search_placeholder()
    if not search_obs:
        raise WeChatForegroundError("切换搜一搜前未找到微信左上搜索框")

    _click_norm(search_obs.cx, search_obs.cy)
    _sleep(0.25, 0.05)
    _paste_text(account_name)
    _sleep(0.5, 0.1)
    _open_soso_search_page(account_name)
    if _looks_like_account_page():
        return
    if not _looks_like_soso_page():
        raise WeChatForegroundError(f"未能打开搜一搜页: {account_name}")

    _apply_soso_public_account_filter(account_name)
    result = _find_soso_public_account_result(account_name)
    if not result:
        _debug_capture(account_name, "missing_soso_account_result")
        _print_visible_texts(account_name, "missing_soso_account_result")
        raise WeChatForegroundError(f"搜一搜账号页未找到公众号: {account_name}")

    _click_norm(result.cx, result.cy)
    _sleep(0.25, 0.05)
    _click_norm(result.cx, result.cy)
    _sleep(1.8, 0.25)


def _looks_like_soso_page() -> bool:
    texts = _ocr()
    visible = " | ".join(item.text for item in texts)
    has_tabs = any(item.text in {"账号", "文章", "视频", "百科", "新闻"} for item in texts)
    has_search = any("搜索" in item.text and item.y > 0.75 for item in texts)
    generic_tabs = any(label in visible for label in ("朋友圈", "视频号", "公众号", "小程序"))
    return (
        (has_tabs and has_search)
        or ("搜一搜" in visible and has_search and generic_tabs)
        or _visible_has_soso_markers(visible)
    )


def _visible_has_soso_markers(visible: str) -> bool:
    return (
        "账号" in visible
        and ("AI搜索" in visible or "Al搜索" in visible)
        and any(label in visible for label in ("问一问", "划线", "听一听", "直播", "相关搜索"))
    )


def _find_search_popup_query(account_name: str) -> OCRText | None:
    matches = [
        item for item in _ocr()
        if account_name in item.text and item.x < 0.35 and item.y > 0.78
    ]
    return sorted(matches, key=lambda item: (-item.y, item.x))[0] if matches else None


def _find_search_popup_network_result(account_name: str) -> OCRText | None:
    query = account_name.lower()
    texts = _ocr()
    network_rows = [
        item for item in texts
        if query in item.text.lower()
        and 0.04 < item.x < 0.30
        and 0.06 < item.y < 0.24
    ]
    if network_rows:
        found = sorted(network_rows, key=lambda item: -item.y)[0]
        return OCRText(found.text, 0.055, found.y - 0.01, 0.22, found.h + 0.03)
    return None


def _apply_soso_public_account_filter(account_name: str) -> None:
    if not _click_soso_tab("账号"):
        raise WeChatForegroundError(f"搜一搜页未找到“账号”筛选: {account_name}")
    _sleep(1.0, 0.2)

    # The second row can already be on "不限"; switching to "公众号" narrows the
    # result list and avoids contacts/groups/chat-record matches.
    if not _click_soso_tab("公众号"):
        raise WeChatForegroundError(f"搜一搜页未找到“公众号”筛选: {account_name}")
    _sleep(1.8, 0.35)


def _click_soso_tab(label: str) -> bool:
    item = _find_text_exact(label)
    if not item:
        return False
    _click_norm(item.cx, item.cy)
    return True


def _find_soso_public_account_result(account_name: str) -> OCRText | None:
    texts: list[OCRText] = []
    for _ in range(4):
        texts = _ocr()
        if any(account_name in item.text for item in texts):
            break
        _sleep(0.6, 0.1)
    exact = [
        item for item in texts
        if item.text.strip() == account_name
        and 0.08 < item.x < 0.80
        and 0.12 < item.y < 0.72
    ]
    if exact:
        found = sorted(exact, key=lambda item: -item.y)[0]
        return found

    contains = [
        item for item in texts
        if account_name in item.text
        and 0.08 < item.x < 0.80
        and 0.12 < item.y < 0.72
        and "搜索" not in item.text
    ]
    if contains:
        found = sorted(contains, key=lambda item: -item.y)[0]
        return found
    visible = " | ".join(item.text for item in texts)
    if "账号" in visible and "公众号" in visible and ("篇原创内容" in visible or "上次使用" in visible):
        # On the account-filtered Soso page the first public-account card sits
        # in a stable band. Use it as a last resort when OCR misses the exact
        # account name but clearly sees an account result list.
        return OCRText(account_name, 0.235, 0.635, 0.10, 0.04)
    return None


def _find_account_result(account_name: str) -> OCRText | None:
    texts = _ocr()
    section = [
        item for item in texts
        if item.text.strip() == "公众号" and item.x < 0.35 and 0.20 < item.y < 0.90
    ]
    if section:
        anchor = sorted(section, key=lambda item: -item.y)[0]
        below_section = [
            item for item in texts
            if account_name in item.text
            and item.x < 0.35
            and item.y < anchor.y
            and item.y > max(anchor.y - 0.18, 0.05)
        ]
        if below_section:
            found = sorted(below_section, key=lambda item: -item.y)[0]
            return OCRText(
                found.text,
                0.075,
                found.y - 0.035,
                0.21,
                found.h + 0.075,
            )

        # OCR can miss the account-name text but still see the "公众号" section.
        # Click the usual first-result row under that section instead of picking
        # an unrelated query suggestion above it.
        return OCRText(account_name, 0.075, max(anchor.y - 0.095, 0.1), 0.21, 0.09)

    exact = [
        item for item in texts
        if item.text.strip() == account_name and item.x < 0.35 and 0.20 < item.y < 0.90
    ]
    if exact:
        return sorted(exact, key=lambda item: -item.y)[0]
    return None


def _find_next_article_on_account_page(
    date_labels: list[str],
    visited_titles: set[str],
) -> OCRText | None:
    texts = _ocr()
    separators = [
        item for item in texts
        if 0.32 < item.x < 0.75
        and (
            item.text in {"今天", "昨天"}
            or item.text.startswith("星期")
            or ("年" in item.text and "月" in item.text and "日" in item.text)
        )
    ]
    target_separators = [
        item for item in separators
        if any(label in item.text for label in date_labels)
    ]
    sections: list[tuple[float, float]] = []
    for anchor in sorted(target_separators, key=lambda item: -item.y):
        next_separators = [item for item in separators if item.y < anchor.y]
        lower_bound = max((item.y for item in next_separators), default=0.0)
        sections.append((anchor.y, lower_bound))

    if not sections and "今天" in date_labels:
        # Some account pages show today's cards as HH:MM without a "今天"
        # separator. Treat the cards below the profile area and above "昨天"
        # as today's articles.
        yesterday = [
            item for item in texts
            if "昨天" in item.text
            and 0.32 < item.x < 0.75
            and item.y < 0.68
        ]
        sections.append((0.68, max((item.y for item in yesterday), default=0.0)))

    if not sections:
        return None

    excluded = (
        "阅读", "赞", "朋友看过", "已关注", "发消息", "全部", "贴图",
        "视频号", "公众号", "投稿合作", "专栏报告", "AI大会",
        "比赛信息", "课程学习", "联系我们", "rename", "Go to", "SKILL",
        "main", "scripts", "tech-sharing", "Textin", "TextIn",
    )
    raw_candidates = [
        item for item in texts
        if 0.32 < item.x < 0.57
        and any(item.y < upper and item.y > lower for upper, lower in sections)
        and len(item.text) >= 6
        and item.w >= 0.08
        and re.search(r"[\u4e00-\u9fff]", item.text)
        and "/" not in item.text
        and "->" not in item.text
        and not any(word in item.text for word in excluded)
        and item.text not in visited_titles
    ]
    candidates: list[OCRText] = []
    for item in sorted(raw_candidates, key=lambda candidate: -candidate.y):
        # Skip continuation/snippet lines from the same article card.
        if any(abs(item.x - prev.x) < 0.14 and 0 < prev.y - item.y < 0.10 for prev in candidates):
            continue
        candidates.append(item)
    return sorted(candidates, key=lambda item: -item.y)[0] if candidates else None


def _collect_wechat_article_urls_windows(
    account_name: str,
    max_articles: int = 10,
    days: int = 1,
    target_date: str | date | None = None,
    prompt_user: bool = True,
) -> list[WeChatArticleURL]:
    """Collect article URLs from the visible Windows WeChat client.

    Windows WeChat does not expose article pages as browser DOM either, so this
    follows the same short foreground-takeover contract as macOS. It first uses
    Microsoft UI Automation for visible text, then falls back to standardized
    window-relative click points when WeChat does not expose a text node.
    """
    target = _coerce_target_date(target_date)
    date_labels = _date_labels_for_window(target, days)
    _win_reset_visual_session()
    _win_set_window_title_hint("")
    _win_set_window_process_hint("Weixin")

    windows_strategy = os.getenv("WECHAT_WINDOWS_FOREGROUND_STRATEGY", "").strip() or "cache"
    print(f"[wechat_foreground] Windows foreground strategy: {windows_strategy}", flush=True)
    cache_fallback_enabled = os.getenv("WECHAT_WINDOWS_ALLOW_CACHE_FALLBACK", "").strip() == "1"

    if not _win_foreground_clicks_enabled():
        print(
            "[wechat_foreground] Windows foreground clicks are disabled by default because they can white-screen WeChat; "
            "using cache fallback only. Set WECHAT_WINDOWS_FOREGROUND_STRATEGY=visual for screenshot-first foreground collection. "
            f"Current WECHAT_WINDOWS_FOREGROUND_STRATEGY={windows_strategy!r}.",
            flush=True,
        )
        if not cache_fallback_enabled:
            raise WeChatForegroundError(
                "Windows foreground collection is disabled and cache fallback is disabled."
            )
        cached = _collect_wechat_article_urls_windows_cache(
            account_name=account_name,
            max_articles=max_articles,
            target_date=target,
            days=days,
        )
        if cached:
            return cached
        raise WeChatForegroundError(
            "Windows foreground clicks are disabled and no matching WeChat cache URL was found."
        )

    visual_strategy = _win_visual_strategy_enabled()
    if visual_strategy and os.getenv("WECHAT_WINDOWS_REQUIRE_VISUAL_PREFLIGHT", "1").strip() != "0":
        _win_require_visual_preflight(account_name)

    user_confirmed_foreground = False
    if prompt_user and _is_interactive():
        print(
            "\n[wechat_foreground] 即将接管 Windows 微信前台 20-60 秒。"
            "请确保微信已登录，并暂时不要操作鼠标键盘。"
        )
        input("[wechat_foreground] 准备好后按 Enter 继续；按 Ctrl+C 取消...")

    if prompt_user and _is_interactive():
        user_confirmed_foreground = True
        os.environ.setdefault("WECHAT_WINDOWS_ENABLE_DEEPLINK", "1")

    foreground_error: WeChatForegroundError | None = None
    if not visual_strategy:
        try:
            _win_activate_wechat()
        except WeChatForegroundError as exc:
            if _win_search_deeplink_enabled(user_confirmed_foreground):
                print(f"[wechat_foreground] Windows 前台接管不可用，尝试微信搜索深链唤起: {exc}", flush=True)
                _win_open_search_deeplink(account_name)
                _sleep(3.0, 0.3)
                try:
                    _win_activate_wechat()
                except WeChatForegroundError as retry_exc:
                    foreground_error = retry_exc
                else:
                    print("[wechat_foreground] Windows 搜索深链唤起成功，继续前台采集", flush=True)
            else:
                print("[wechat_foreground] Windows 前台接管不可用，深链唤起未启用。", flush=True)
                foreground_error = exc

    if foreground_error is not None:
        if not cache_fallback_enabled:
            raise foreground_error
        print(f"[wechat_foreground] Windows 前台接管不可用，尝试缓存 URL fallback: {foreground_error}", flush=True)
        cached = _collect_wechat_article_urls_windows_cache(
            account_name=account_name,
            max_articles=max_articles,
            target_date=target,
            days=days,
        )
        if cached:
            print(f"[wechat_foreground] Windows 缓存 fallback 获取 URL: {len(cached)} 篇", flush=True)
            return cached
        raise foreground_error
    if not visual_strategy:
        try:
            _win_assert_renderable(account_name, "before_standardize")
        except WeChatForegroundError as exc:
            if not cache_fallback_enabled:
                raise exc
            cached = _collect_wechat_article_urls_windows_cache(
                account_name=account_name,
                max_articles=max_articles,
                target_date=target,
                days=days,
            )
            if cached:
                print(f"[wechat_foreground] Windows 主窗口不可渲染，缓存 fallback 获取 URL: {len(cached)} 篇", flush=True)
                return cached
            raise exc
        _win_standardize_wechat_window()
        _sleep(0.6, 0.12)
        _win_assert_renderable(account_name, "activated")

    print(f"[wechat_foreground] Windows 搜索公众号: {account_name}", flush=True)
    if not visual_strategy:
        _win_prepare_visual_step("search")
    _win_search_account(account_name)
    _sleep(_win_env_float("WECHAT_WINDOWS_AFTER_SEARCH_WAIT", 0.65), 0.12)
    _win_debug_capture(account_name, "after_search")

    _win_prepare_visual_step("open_account_result")
    if not _win_open_account_result(account_name):
        _win_print_visible_texts(account_name, "missing_account_result")
        raise WeChatForegroundError(f"Windows 微信未找到公众号搜索结果: {account_name}")

    _sleep(1.6, 0.25)
    _win_mark_account_window(account_name)
    _win_debug_capture(account_name, "account_result_opened")
    _win_ensure_full_account_home(account_name)
    _win_debug_capture(account_name, "account_page")

    urls: list[WeChatArticleURL] = []
    visited: set[str] = set()
    seen_urls: set[str] = set()
    account_handle = _win_foreground_handle() if _win_visual_strategy_enabled() else 0
    if account_handle:
        _win_assert_wechat_window_handle(account_handle, "account window")

    try:
        for attempt in range(max_articles * 3):
            if len(urls) >= max_articles:
                break

            if _win_visual_strategy_enabled() and account_handle:
                _win_focus_known_window(account_handle)
                _sleep(0.4, 0.08)

            article = _win_find_next_article_on_account_page(date_labels, visited)
            if article:
                title = article.text
                visited.add(title)
                print(f"[wechat_foreground] Windows 打开文章: {title}", flush=True)
                _win_prepare_visual_step("open_article")
                _win_click_norm(article.cx, article.cy)
            else:
                fallback_index = len(visited)
                title = f"{account_name} 微信文章 {fallback_index + 1}"
                visited.add(title)
                print(f"[wechat_foreground] Windows 使用坐标回退打开文章: {title}", flush=True)
                _win_prepare_visual_step("open_article_fallback")
                if not _win_open_article_by_index(fallback_index):
                    _win_print_visible_texts(account_name, "missing_article")
                    break

            _win_set_window_title_hint("")
            _win_set_window_process_hint("WeChatAppEx")
            _sleep(3.0, 0.4)
            article_handle = _win_foreground_handle() if _win_visual_strategy_enabled() else 0
            if article_handle:
                _win_assert_wechat_window_handle(article_handle, "article window")
            if _win_visual_strategy_enabled() and article_handle == account_handle:
                print("[wechat_foreground] Windows 文章窗口未打开，跳过当前卡片继续尝试。", flush=True)
                _win_set_window_title_hint(account_name)
                _win_set_window_process_hint("Weixin")
                continue
            _win_debug_capture(account_name, f"article_page_{attempt + 1}")
            _win_prepare_visual_step("copy_article_url")
            url = _win_copy_visible_article_url(article_handle if _win_visual_strategy_enabled() else None)
            url = _extract_mp_weixin_url(url)

            if "mp.weixin.qq.com" in url and url not in seen_urls:
                print(f"[wechat_foreground] Windows 获取 URL: {url[:90]}", flush=True)
                published_date, date_ok = _verified_wechat_article_date(url, target, days)
                if not date_ok:
                    label = published_date.isoformat() if published_date else "unknown"
                    print(
                        f"[wechat_foreground] Windows 跳过非目标日期文章: {label} {url[:90]}",
                        flush=True,
                    )
                    seen_urls.add(url)
                    _win_restore_account_after_article(account_name, account_handle, article_handle)
                    _sleep(1.0, 0.2)
                    continue
                seen_urls.add(url)
                urls.append(
                    WeChatArticleURL(
                        title=title,
                        url=url,
                        published_at=(published_date or target).isoformat(),
                        snippet=f"Windows 微信前台采集: {account_name}",
                    )
                )
            elif url in seen_urls:
                print(f"[wechat_foreground] Windows 跳过重复 URL: {url[:90]}", flush=True)
            else:
                print("[wechat_foreground] Windows 未复制到微信文章 URL", flush=True)

            _win_set_window_title_hint(account_name)
            _win_set_window_process_hint("Weixin")
            _win_restore_account_after_article(account_name, account_handle, article_handle)
            _sleep(1.0, 0.2)
    finally:
        if _win_visual_strategy_enabled():
            _win_cleanup_after_account_visual(account_handle)
        else:
            _win_cleanup_after_account()

    if prompt_user:
        print("[wechat_foreground] Windows 微信前台接管结束，可以继续使用电脑。", flush=True)
    return urls


def _win_restore_account_after_article(account_name: str, account_handle: int, article_handle: int) -> None:
    """Return Windows collection to the known account window without global shortcuts."""
    _win_set_window_title_hint(account_name)
    _win_set_window_process_hint("Weixin")
    if _win_visual_strategy_enabled():
        if article_handle and article_handle != account_handle:
            _win_close_known_window(article_handle)
            _sleep(0.8, 0.15)
        if account_handle:
            _win_focus_known_window(account_handle)
        return
    _win_return_to_account_page()
    _win_ensure_full_account_home(account_name, soft=True)


def _win_cleanup_after_account_visual(account_handle: int) -> None:
    """Close the known account popup and return to the existing main WeChat window."""
    if os.getenv("WECHAT_FOREGROUND_KEEP_ACCOUNT_WINDOW", "").strip() == "1":
        return
    _win_set_window_title_hint("")
    _win_set_window_process_hint("Weixin")
    main_handle = 0
    try:
        main_handle = _win_choose_existing_wechat_window_handle()
    except Exception:
        main_handle = 0

    try:
        if account_handle and account_handle != main_handle:
            _win_close_known_window(account_handle)
            _sleep(0.5, 0.1)
    except WeChatForegroundError as exc:
        print(f"[wechat_foreground] Windows visual cleanup skipped account window close: {exc}", flush=True)

    if main_handle:
        try:
            _win_focus_known_window(main_handle)
        except WeChatForegroundError as exc:
            print(f"[wechat_foreground] Windows visual cleanup could not focus main WeChat: {exc}", flush=True)


def _collect_wechat_article_urls_windows_cache(
    account_name: str,
    max_articles: int = 10,
    target_date: str | date | None = None,
    days: int = 1,
) -> list[WeChatArticleURL]:
    """Extract recent mp.weixin.qq.com article URLs from Windows WeChat cache."""
    biz_hints = _windows_cache_biz_hints(account_name)
    candidates = _scan_windows_wechat_cache_urls(limit=max(max_articles * 1000, 5000))
    urls = _filter_windows_cache_candidates(
        account_name,
        candidates,
        max_articles,
        allow_biz=True,
        target_date=target_date,
        days=days,
    )
    if urls:
        return urls

    if biz_hints and os.getenv("WECHAT_WINDOWS_CACHE_ALLOW_STALE_BIZ", "") == "1":
        max_age = _win_env_int("WECHAT_WINDOWS_CACHE_BIZ_MAX_AGE_DAYS", 1095)
        candidates = _scan_windows_wechat_cache_urls(
            limit=max(max_articles * 1000, 5000),
            max_age_days=max_age,
        )
        return _filter_windows_cache_candidates(
            account_name,
            candidates,
            max_articles,
            allow_biz=True,
            target_date=target_date,
            days=days,
        )
    return []


def _filter_windows_cache_candidates(
    account_name: str,
    candidates: list[dict[str, object]],
    max_articles: int,
    allow_biz: bool = True,
    target_date: str | date | None = None,
    days: int = 1,
) -> list[WeChatArticleURL]:
    urls: list[WeChatArticleURL] = []
    seen: set[str] = set()
    query = account_name.lower()
    biz_hints = _windows_cache_biz_hints(account_name) if allow_biz else set()
    target = _coerce_target_date(target_date)
    for item in candidates:
        url = str(item.get("url", ""))
        key = _wechat_article_key(url)
        if not url or key in seen:
            continue
        title = str(item.get("title", "")).strip()
        brand = str(item.get("brand_name", "")).strip()
        source_path = str(item.get("path", ""))
        haystack = f"{brand} {title} {source_path} {url}".lower()
        url_biz = _wechat_url_biz(url)
        matches_name = bool(query and query in haystack)
        matches_biz = bool(url_biz and url_biz in biz_hints)
        if not (matches_name or matches_biz) and os.getenv("WECHAT_WINDOWS_CACHE_ALLOW_RECENT", "") != "1":
            continue
        published_date, date_ok = _verified_wechat_article_date(url, target, days)
        if not date_ok:
            continue
        seen.add(key)
        urls.append(
            WeChatArticleURL(
                title=title or f"{brand or account_name} Windows 微信缓存文章 {len(urls) + 1}",
                url=url,
                published_at=(published_date or target).isoformat(),
                snippet=f"Windows 微信缓存采集: {brand or account_name}",
            )
        )
        if len(urls) >= max_articles:
            break
    return urls


def _cache_item_date(item: dict[str, object]) -> str:
    try:
        mtime = float(item.get("mtime", 0.0))
    except (TypeError, ValueError):
        mtime = 0.0
    if mtime > 0:
        return datetime.fromtimestamp(mtime).date().isoformat()
    return date.today().isoformat()


def _windows_cache_biz_hints(account_name: str) -> set[str]:
    hints: set[str] = set()
    configured = os.getenv("WECHAT_WINDOWS_CACHE_BIZ_HINTS", "")
    if configured:
        try:
            data = json.loads(configured)
            values = data.get(account_name, []) if isinstance(data, dict) else []
            if isinstance(values, str):
                values = [values]
            hints.update(str(value) for value in values if value)
        except json.JSONDecodeError:
            pass

    # Derived from the local Windows WeChat cache for the configured source.
    # It keeps the cache fallback account-specific when metadata is missing.
    builtins = {
        "量子位": ["MzA4ODM5MTQwMQ=="],
        "QbitAI": ["MzA4ODM5MTQwMQ=="],
    }
    hints.update(builtins.get(account_name, []))
    return hints


def _wechat_url_biz(url: str) -> str:
    try:
        split = urlsplit(url)
    except ValueError:
        return ""
    params = dict(parse_qsl(split.query, keep_blank_values=False))
    return params.get("__biz", "")


def _wechat_article_key(url: str) -> str:
    try:
        split = urlsplit(url)
    except ValueError:
        return url
    params = dict(parse_qsl(split.query, keep_blank_values=False))
    parts = [params.get(key, "") for key in ("__biz", "mid", "idx", "sn")]
    return "|".join(parts) if all(parts) else url


def _scan_windows_wechat_cache_urls(limit: int = 50, max_age_days: int = 30) -> list[dict[str, object]]:
    roots = _windows_wechat_cache_roots()
    found: dict[str, dict[str, object]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in _iter_recent_cache_files(root, max_age_days=max_age_days):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            for entry in _extract_wechat_article_entries_from_bytes(data):
                url = str(entry.get("url", ""))
                if not url:
                    continue
                previous = found.get(url)
                mtime = path.stat().st_mtime
                if previous:
                    previous_has_meta = bool(previous.get("brand_name") or previous.get("title"))
                    entry_has_meta = bool(entry.get("brand_name") or entry.get("title"))
                    if previous_has_meta and not entry_has_meta:
                        previous["mtime"] = max(float(previous.get("mtime", 0.0)), mtime)
                        continue
                    if float(previous.get("mtime", 0.0)) >= mtime and previous_has_meta == entry_has_meta:
                        continue
                found[url] = {
                    "url": url,
                    "title": entry.get("title", ""),
                    "brand_name": entry.get("brand_name", ""),
                    "digest": entry.get("digest", ""),
                    "path": str(path),
                    "mtime": mtime,
                }
    return sorted(found.values(), key=lambda item: float(item.get("mtime", 0.0)), reverse=True)[:limit]


def _windows_wechat_cache_roots() -> list[Path]:
    appdata = os.getenv("APPDATA")
    if not appdata:
        return []
    base = Path(appdata) / "Tencent"
    return [
        base / "xwechat" / "radium" / "web" / "profiles",
        base / "WeChat" / "xweb",
        base / "WeChat" / "radium",
    ]


def _iter_recent_cache_files(root: Path, max_age_days: int = 30, max_size_mb: int = 24):
    cutoff = time.time() - max_age_days * 86400
    try:
        paths = root.rglob("*")
    except OSError:
        return
    for path in paths:
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff or stat.st_size <= 0:
            continue
        if stat.st_size > max_size_mb * 1024 * 1024:
            continue
        name = path.name.lower()
        if name == "lock" or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".pak", ".dll", ".exe")):
            continue
        yield path


_WECHAT_CACHE_URL_RE = re.compile(
    rb"https?://mp\.weixin\.qq\.com/s\?[A-Za-z0-9_%&=./:#~+\-]+"
)


def _extract_wechat_article_entries_from_bytes(data: bytes) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in _WECHAT_CACHE_URL_RE.finditer(data):
        raw = match.group(0).decode("ascii", errors="ignore")
        url = _normalize_wechat_article_url(raw)
        if url and url not in seen:
            seen.add(url)
            context = data[max(0, match.start() - 2048): min(len(data), match.end() + 16384)].decode(
                "utf-8",
                errors="ignore",
            )
            entries.append(
                {
                    "url": url,
                    "brand_name": _extract_jsonish_string(context, "brandName"),
                    "title": _extract_jsonish_string(context, "title"),
                    "digest": _extract_jsonish_string(context, "digest"),
                }
            )
    return entries


def _extract_jsonish_string(text: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\]){{1,500}})"', text)
    if not match:
        return ""
    value = match.group(1)
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def _normalize_wechat_article_url(raw_url: str) -> str:
    marker = "https://"
    second = raw_url.find(marker, len(marker))
    if second > 0:
        raw_url = raw_url[:second]
    raw_url = raw_url.rstrip(".,;:)]}'\"")
    try:
        split = urlsplit(raw_url)
    except ValueError:
        return ""
    if split.netloc != "mp.weixin.qq.com" or split.path != "/s":
        return ""
    params = dict(parse_qsl(split.query, keep_blank_values=False))
    required = ("__biz", "mid", "idx", "sn")
    if not all(params.get(key) for key in required):
        return ""
    keep = ["__biz", "mid", "idx", "sn", "chksm", "scene", "subscene"]
    query = urlencode([(key, params[key]) for key in keep if params.get(key)])
    return urlunsplit(("https", "mp.weixin.qq.com", "/s", query, "rd"))


def _win_powershell(script: str, *args: str, timeout: float = 15.0) -> str:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    script_path = TMP_DIR / f"wechat_foreground_{os.getpid()}_{int(time.time() * 1000)}.ps1"
    script_path.write_text(script, encoding="utf-8-sig")
    try:
        return _run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), *args],
            timeout=timeout,
        )
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


def _win_process_names() -> str:
    return os.getenv("WECHAT_WINDOWS_PROCESS", "Weixin,WeChat,微信,WeChatAppEx")


def _win_require_visual_preflight(account_name: str) -> None:
    preflight = preflight_windows_wechat_visual(capture=True)
    foreground_maximize_error: WeChatForegroundError | None = None
    initial_visual_checked = False
    if not preflight.get("ready") and _win_preflight_blocker(preflight) == "foreground_wechat_window_not_maximized":
        try:
            _win_ensure_foreground_visual_maximized(account_name, verify_renderable=False)
        except WeChatForegroundError as exc:
            foreground_maximize_error = exc
        else:
            initial_visual_checked = True
            _sleep(_win_env_float("WECHAT_WINDOWS_AFTER_INITIAL_MAXIMIZE_WAIT", 0.35), 0.08)
            preflight = preflight_windows_wechat_visual(capture=True)
    if not preflight.get("ready") and _win_visual_auto_reveal_enabled() and _win_preflight_can_auto_reveal(preflight):
        _win_auto_reveal_wechat_visual(account_name)
        initial_visual_checked = True
        _sleep(_win_env_float("WECHAT_WINDOWS_AFTER_AUTO_REVEAL_WAIT", 0.7), 0.12)
        preflight = preflight_windows_wechat_visual(capture=True)
    if not preflight.get("ready") and _win_preflight_blocker(preflight) == "foreground_wechat_window_not_maximized":
        _win_ensure_foreground_visual_maximized(account_name, verify_renderable=False)
        initial_visual_checked = True
        _sleep(_win_env_float("WECHAT_WINDOWS_AFTER_INITIAL_MAXIMIZE_WAIT", 0.35), 0.08)
        preflight = preflight_windows_wechat_visual(capture=True)
    if preflight.get("ready"):
        if not initial_visual_checked:
            _win_ensure_initial_visual_maximized(account_name)
        return
    if foreground_maximize_error is not None and not _win_visual_auto_reveal_enabled():
        raise foreground_maximize_error
    blockers = "; ".join(str(item) for item in preflight.get("blockers", []))
    raise WeChatForegroundError(
        "Windows visual preflight failed; refusing foreground clicks to avoid white-screening "
        f"WeChat or closing another app. Blockers: {blockers or 'unknown'}"
    )


def _win_visual_auto_reveal_enabled() -> bool:
    return os.getenv("WECHAT_WINDOWS_AUTO_REVEAL", "1").strip() != "0"


def _win_preflight_blocker(preflight: dict[str, object]) -> str:
    diagnosis = preflight.get("diagnosis", {})
    if not isinstance(diagnosis, dict):
        return ""
    return str(diagnosis.get("foreground_blocker", ""))


def _win_preflight_can_auto_reveal(preflight: dict[str, object]) -> bool:
    diagnosis = preflight.get("diagnosis", {})
    if not isinstance(diagnosis, dict):
        return False
    blocker = str(diagnosis.get("foreground_blocker", ""))
    if bool(diagnosis.get("screen_capture_black")):
        return False
    return blocker in {
        "no_visible_wechat_window",
        "foreground_not_wechat",
        "foreground_wechat_window_not_maximized",
        "uia_text_unavailable",
    }


def _win_allowed_process_names() -> set[str]:
    names = {name.strip().lower() for name in _win_process_names().split(",") if name.strip()}
    names.update({"weixin", "wechat", "wechatappex"})
    return names


def _win_process_name_is_wechat(process_name: str) -> bool:
    return process_name.strip().lower() in _win_allowed_process_names()


def _win_foreground_process_name() -> str:
    handle = _win_foreground_handle()
    if not handle:
        return ""
    try:
        return _win_window_process_name_for_handle(handle)
    except Exception:
        return ""


def _win_ui_text_has_content(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return False
    shell_labels = {"Weixin", "WeChat", "微信"}
    if normalized in shell_labels:
        return False
    if normalized.startswith("Weixin ") or normalized.startswith("WeChat "):
        return False
    return True


def _win_content_texts(texts: list[OCRText]) -> list[OCRText]:
    return [item for item in texts if _win_ui_text_has_content(item.text)]


def _win_screenshot_looks_black(stats: dict[str, object]) -> bool:
    return bool(
        stats
        and float(stats.get("black_ratio", 0.0)) >= 0.98
        and float(stats.get("avg_luma", 255.0)) <= 3.0
    )


def _win_screenshot_looks_white_without_content(stats: dict[str, object], texts: list[OCRText]) -> bool:
    return bool(
        stats
        and float(stats.get("black_ratio", 1.0)) <= 0.02
        and float(stats.get("avg_luma", 0.0)) >= 245.0
        and not _win_content_texts(texts)
    )


def _win_window_usable_for_visual(info: WindowsWindowInfo | None) -> bool:
    if info is None:
        return False
    return not _win_window_info_looks_minimized(info) and info.width >= 560 and info.height >= 500


def _win_launch_wechat_if_needed(force: bool = False) -> None:
    """Bring up the real WeChat app window before any search-box automation."""
    if not force and os.getenv("WECHAT_WINDOWS_ALLOW_LAUNCH", "").strip() != "1":
        return
    candidates = [
        os.getenv("WECHAT_WINDOWS_EXE", "").strip(),
        os.getenv("WECHAT_WINDOWS_EXE_PATH", "").strip(),
        *_win_running_wechat_exe_paths(),
        r"D:\Weixin\Weixin.exe",
        r"C:\Program Files\Tencent\WeChat\WeChat.exe",
        r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
    ]
    exe = next((path for path in candidates if path and Path(path).exists()), "")
    if not exe:
        return
    try:
        _win_powershell("Start-Process -FilePath $args[0]", exe, timeout=6.0)
        _sleep(1.2, 0.2)
    except Exception as exc:
        print(f"[wechat_foreground] Windows launch WeChat skipped: {exc}", flush=True)


def _win_running_wechat_exe_paths() -> list[str]:
    script = r'''
$names = $args[0] -split ','
$paths = New-Object System.Collections.Generic.List[string]
foreach ($name in $names) {
  $procs = Get-Process -Name $name.Trim() -ErrorAction SilentlyContinue
  foreach ($proc in $procs) {
    try {
      if ($proc.Path -and -not $paths.Contains($proc.Path)) {
        $paths.Add($proc.Path) | Out-Null
      }
    } catch {}
  }
}
$paths
'''
    try:
        out = _win_powershell(script, _win_process_names(), timeout=6.0)
    except Exception:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _win_auto_reveal_wechat_visual(account_name: str) -> None:
    """Bring an existing WeChat window forward before visual clicks."""
    print("[wechat_foreground] Windows visual preflight blocked; trying automatic WeChat reveal.", flush=True)
    _win_reveal_existing_wechat_window()
    _sleep(_win_env_float("WECHAT_WINDOWS_AFTER_RESTORE_WAIT", 0.45), 0.08)
    _win_ensure_initial_visual_maximized(account_name, verify_renderable=False)
    if os.getenv("WECHAT_WINDOWS_AUTO_REVEAL_USE_DEEPLINK", "").strip() == "1":
        _win_open_search_deeplink(account_name, force=True)
        _sleep(1.6, 0.25)
    if os.getenv("WECHAT_WINDOWS_AUTO_REVEAL_ALLOW_LAUNCH", "").strip() == "1":
        _win_launch_wechat_if_needed(force=True)


def _win_reveal_existing_wechat_window() -> None:
    handle = _win_choose_existing_wechat_window_handle()
    if not handle:
        print("[wechat_foreground] Windows auto reveal found no existing WeChat window handle.", flush=True)
        return
    try:
        _win_assert_wechat_window_handle(handle, "auto reveal")
        _win_restore_existing_window(handle)
    except Exception as exc:
        print(f"[wechat_foreground] Windows auto reveal skipped existing window restore: {exc}", flush=True)


def _win_choose_existing_wechat_window_handle() -> int:
    try:
        info = _win_wechat_window_info(activate=False)
        if _win_window_usable_for_visual(info):
            return info.handle
    except Exception:
        pass

    probe = _win_window_probe()
    candidates: list[tuple[int, int]] = []
    for item in probe:
        try:
            handle = int(item.get("main_window_handle", 0))
            if handle <= 0:
                continue
            all_windows = int(item.get("all_top_windows", 0))
            visible_windows = int(item.get("visible_top_windows", 0))
        except (TypeError, ValueError):
            continue
        # A logged-in WeChat session usually owns many top-level helper windows,
        # while a newly spawned login shell owns only a few. Prefer the richer
        # existing session instead of creating or selecting a fresh login shell.
        score = (all_windows * 100) + visible_windows
        candidates.append((score, handle))
    if not candidates:
        return 0
    return sorted(candidates, reverse=True)[0][1]


def _win_restore_existing_window(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = ctypes.c_void_p(handle)
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL

    # Restore/show only the existing top-level window. Fullscreen is handled by
    # a separate visible title-bar click so it cannot silently launch/resize a
    # different WeChat instance.
    sw_restore = 9
    sw_show = 5
    user32.ShowWindowAsync(hwnd, sw_restore if user32.IsIconic(hwnd) else sw_show)
    hwnd_top = ctypes.c_void_p(0)
    swp_nomove = 0x0002
    swp_nosize = 0x0001
    swp_showwindow = 0x0040
    user32.SetWindowPos(hwnd, hwnd_top, 0, 0, 0, 0, swp_nomove | swp_nosize | swp_showwindow)
    user32.SetForegroundWindow(hwnd)


def diagnose_windows_wechat_foreground(capture: bool = True) -> dict[str, object]:
    """Return a no-click Windows WeChat foreground diagnostic snapshot."""
    if sys.platform != "win32":
        return {"platform": sys.platform, "supported": False}

    probe = _win_window_probe()
    visible_windows = sum(int(item.get("visible_top_windows", 0)) for item in probe)
    all_windows = sum(int(item.get("all_top_windows", 0)) for item in probe)
    foreground_handle = _win_foreground_handle()
    foreground_process = _win_foreground_process_name()
    foreground_is_wechat = _win_process_name_is_wechat(foreground_process)
    foreground_info: WindowsWindowInfo | None = None
    if foreground_handle:
        try:
            foreground_info = _win_window_info_for_handle(foreground_handle)
        except Exception:
            foreground_info = None
    foreground_maximized = bool(foreground_info and _win_window_looks_maximized(foreground_info))
    screenshot = ""
    screenshot_stats: dict[str, object] = {}
    if capture:
        before = set(DEBUG_DIR.glob("*_windows_diagnose_*.png")) if DEBUG_DIR.exists() else set()
        _win_debug_capture("windows_diagnose", "safe_probe")
        after = set(DEBUG_DIR.glob("*_windows_diagnose_*.png")) if DEBUG_DIR.exists() else set()
        created = sorted(after - before, key=lambda path: path.stat().st_mtime, reverse=True)
        screenshot = str(created[0]) if created else ""
        if screenshot:
            screenshot_stats = _win_screenshot_stats(Path(screenshot))

    texts: list[OCRText] = []
    if visible_windows:
        # UI Automation text enumeration is slow in Windows WeChat. Use the
        # screenshot first; only ask UIA for text when there is no capture or
        # the capture is bright enough to need a white-screen/content check.
        needs_text_probe = not capture or not screenshot_stats or (
            float(screenshot_stats.get("black_ratio", 1.0)) <= 0.02
            and float(screenshot_stats.get("avg_luma", 0.0)) >= 245.0
        )
        if needs_text_probe:
            texts = _win_texts()

    content_texts = _win_content_texts(texts)
    screen_black = _win_screenshot_looks_black(screenshot_stats)
    screen_white = _win_screenshot_looks_white_without_content(screenshot_stats, texts)
    if visible_windows <= 0:
        blocker = "no_visible_wechat_window"
    elif not foreground_is_wechat:
        blocker = "foreground_not_wechat"
    elif screen_black:
        blocker = "screen_capture_black"
    elif screen_white:
        blocker = "screen_capture_white"
    elif not _win_window_usable_for_visual(foreground_info):
        blocker = "foreground_wechat_window_too_small"
    elif not foreground_maximized:
        blocker = "foreground_wechat_window_not_maximized"
    elif not content_texts and not screenshot_stats:
        blocker = "uia_text_unavailable"
    else:
        blocker = ""

    return {
        "platform": sys.platform,
        "supported": True,
        "process_names": _win_process_names(),
        "processes": probe,
        "all_top_windows": all_windows,
        "visible_top_windows": visible_windows,
        "foreground_handle": foreground_handle,
        "foreground_process": foreground_process,
        "foreground_is_wechat": foreground_is_wechat,
        "foreground_window": foreground_info.__dict__ if foreground_info else {},
        "foreground_window_maximized": foreground_maximized,
        "screen_capture_black": screen_black,
        "screen_capture_white": screen_white,
        "foreground_available": blocker == "",
        "foreground_blocker": blocker,
        "uia_text_count": len(texts),
        "uia_content_text_count": len(content_texts),
        "uia_text_preview": [item.text for item in sorted(texts, key=lambda item: (-item.y, item.x))[:24]],
        "debug_screenshot": screenshot,
        "debug_screenshot_stats": screenshot_stats,
    }


def preflight_windows_wechat_visual(capture: bool = True) -> dict[str, object]:
    """No-click readiness check for the Windows visual foreground strategy."""
    diagnosis = diagnose_windows_wechat_foreground(capture=capture)
    strategy = os.getenv("WECHAT_WINDOWS_FOREGROUND_STRATEGY", "").strip() or "cache"
    cache_fallback_enabled = os.getenv("WECHAT_WINDOWS_ALLOW_CACHE_FALLBACK", "").strip() == "1"
    visual_enabled = strategy.lower() == "visual"
    date_verify_enabled = os.getenv("WECHAT_FOREGROUND_REQUIRE_DATE_VERIFY", "1").strip() != "0"
    max_articles = _win_env_int("WECHAT_FOREGROUND_MAX_ARTICLES", 10)

    blockers: list[str] = []
    if not visual_enabled:
        blockers.append("WECHAT_WINDOWS_FOREGROUND_STRATEGY must be visual")
    if cache_fallback_enabled:
        blockers.append("WECHAT_WINDOWS_ALLOW_CACHE_FALLBACK should be 0 for real foreground testing")
    if not date_verify_enabled:
        blockers.append("WECHAT_FOREGROUND_REQUIRE_DATE_VERIFY should be 1 to avoid wrong-day URLs")
    if max_articles < 2:
        blockers.append("WECHAT_FOREGROUND_MAX_ARTICLES should be >= 2 for multi-article days")

    foreground_blocker = str(diagnosis.get("foreground_blocker", ""))
    if foreground_blocker:
        blockers.append(f"foreground not ready: {foreground_blocker}")

    return {
        "ready": not blockers,
        "blockers": blockers,
        "visual_strategy": strategy,
        "cache_fallback_enabled": cache_fallback_enabled,
        "date_verify_enabled": date_verify_enabled,
        "max_articles": max_articles,
        "diagnosis": diagnosis,
    }


def _win_window_probe() -> list[dict[str, object]]:
    script = r'''
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WeChatProbeWin32 {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
[WeChatProbeWin32]::SetProcessDPIAware() | Out-Null
$names = $args[0] -split ','
foreach ($name in $names) {
  $procs = Get-Process -Name $name.Trim() -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending
  foreach ($proc in $procs) {
    $callback = [WeChatProbeWin32+EnumWindowsProc]{
      param([IntPtr]$hWnd, [IntPtr]$lParam)
      $windowPid = 0
      [WeChatProbeWin32]::GetWindowThreadProcessId($hWnd, [ref]$windowPid) | Out-Null
      if ($windowPid -eq $proc.Id) {
        $script:allCount += 1
        $rect = New-Object WeChatProbeWin32+RECT
        [WeChatProbeWin32]::GetWindowRect($hWnd, [ref]$rect) | Out-Null
        $area = [Math]::Max(0, $rect.Right - $rect.Left) * [Math]::Max(0, $rect.Bottom - $rect.Top)
        if ([WeChatProbeWin32]::IsWindowVisible($hWnd) -and $area -gt 10000) {
          $script:visibleCount += 1
        }
      }
      return $true
    }
    $script:allCount = 0
    $script:visibleCount = 0
    [WeChatProbeWin32]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
    "{0}`t{1}`t{2}`t{3}`t{4}" -f $proc.ProcessName, $proc.Id, $proc.MainWindowHandle, $script:visibleCount, $script:allCount
  }
}
'''
    rows: list[dict[str, object]] = []
    try:
        out = _win_powershell(script, _win_process_names(), timeout=8.0)
    except Exception:
        return rows
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        try:
            rows.append(
                {
                    "process_name": parts[0],
                    "pid": int(parts[1]),
                    "main_window_handle": int(parts[2]),
                    "visible_top_windows": int(parts[3]),
                    "all_top_windows": int(parts[4]),
                }
            )
        except ValueError:
            continue
    return rows


def _win_wechat_window_info(activate: bool = True) -> WindowsWindowInfo:
    if activate and _win_visual_strategy_enabled():
        raise WeChatForegroundError(
            "Windows visual 模式禁止 Win32 强制激活微信；请使用任务栏真实点击路径。"
        )
    script = r'''
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WeChatWindowInfoWin32 {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr SetActiveWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern int GetWindowTextW(IntPtr hWnd, StringBuilder text, int maxCount);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
[WeChatWindowInfoWin32]::SetProcessDPIAware() | Out-Null
$names = $args[0] -split ','
$activate = $args[1] -eq '1'
$titleHint = $args[2]
$processHint = $args[3]
$script:selectedHandle = [IntPtr]::Zero
$script:selectedScore = 0
foreach ($name in $names) {
  $procs = Get-Process -Name $name.Trim() -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending
  foreach ($candidate in $procs) {
    $callback = [WeChatWindowInfoWin32+EnumWindowsProc]{
      param([IntPtr]$hWnd, [IntPtr]$lParam)
      $windowPid = 0
      [WeChatWindowInfoWin32]::GetWindowThreadProcessId($hWnd, [ref]$windowPid) | Out-Null
      $visible = [WeChatWindowInfoWin32]::IsWindowVisible($hWnd)
      $iconic = [WeChatWindowInfoWin32]::IsIconic($hWnd)
      if ($windowPid -eq $candidate.Id) {
        $rect = New-Object WeChatWindowInfoWin32+RECT
        [WeChatWindowInfoWin32]::GetWindowRect($hWnd, [ref]$rect) | Out-Null
        $width = [Math]::Max(0, $rect.Right - $rect.Left)
        $height = [Math]::Max(0, $rect.Bottom - $rect.Top)
        $area = $width * $height
        $sb = New-Object System.Text.StringBuilder 512
        [WeChatWindowInfoWin32]::GetWindowTextW($hWnd, $sb, $sb.Capacity) | Out-Null
        $title = $sb.ToString()
        if ($title -eq "WxTrayIconMessageWindow" -or $title -eq "MSCTFIME UI" -or $title -eq "Default IME") {
          return $true
        }
        if (-not $visible -and -not $iconic -and -not $title -and ($width -lt 250 -or $height -lt 250)) {
          return $true
        }
        $score = $area
        if ($iconic) {
          $score += 300000000
        }
        if ($titleHint -and $title.Contains($titleHint)) {
          $score += 1000000000
        }
        if ($processHint -and $candidate.ProcessName.Contains($processHint)) {
          $score += 500000000
        }
        if ((($width -ge 250 -and $height -ge 250) -or $iconic) -and $score -gt $script:selectedScore) {
          $script:selectedScore = $score
          $script:selectedHandle = $hWnd
        }
      }
      return $true
    }
    [WeChatWindowInfoWin32]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
  }
}
$handle = $script:selectedHandle
if ($handle -eq [IntPtr]::Zero) {
  foreach ($name in $names) {
    $procs = Get-Process -Name $name.Trim() -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending
    foreach ($candidate in $procs) {
      if ($candidate.MainWindowHandle -ne 0) {
        $handle = [IntPtr]$candidate.MainWindowHandle
        break
      }
    }
    if ($handle -ne [IntPtr]::Zero) { break }
  }
}
if ($handle -eq [IntPtr]::Zero) { throw "Windows WeChat top-level window was not found." }
if ($activate) {
  [WeChatWindowInfoWin32]::ShowWindowAsync($handle, 5) | Out-Null
  if ([WeChatWindowInfoWin32]::IsIconic($handle)) {
    [WeChatWindowInfoWin32]::ShowWindowAsync($handle, 9) | Out-Null
  }
  $foreground = [WeChatWindowInfoWin32]::GetForegroundWindow()
  $currentThread = [WeChatWindowInfoWin32]::GetCurrentThreadId()
  $targetPid = 0
  $targetThread = [WeChatWindowInfoWin32]::GetWindowThreadProcessId($handle, [ref]$targetPid)
  $foregroundPid = 0
  $foregroundThread = 0
  if ($foreground -ne [IntPtr]::Zero) {
    $foregroundThread = [WeChatWindowInfoWin32]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid)
  }
  $attachedTarget = $false
  $attachedForeground = $false
  if ($targetThread -ne 0 -and $targetThread -ne $currentThread) {
    $attachedTarget = [WeChatWindowInfoWin32]::AttachThreadInput($currentThread, $targetThread, $true)
  }
  if ($foregroundThread -ne 0 -and $foregroundThread -ne $currentThread -and $foregroundThread -ne $targetThread) {
    $attachedForeground = [WeChatWindowInfoWin32]::AttachThreadInput($currentThread, $foregroundThread, $true)
  }
  [WeChatWindowInfoWin32]::BringWindowToTop($handle) | Out-Null
  [WeChatWindowInfoWin32]::SetForegroundWindow($handle) | Out-Null
  [WeChatWindowInfoWin32]::SetActiveWindow($handle) | Out-Null
  [WeChatWindowInfoWin32]::SetFocus($handle) | Out-Null
  if ($attachedForeground) {
    [WeChatWindowInfoWin32]::AttachThreadInput($currentThread, $foregroundThread, $false) | Out-Null
  }
  if ($attachedTarget) {
    [WeChatWindowInfoWin32]::AttachThreadInput($currentThread, $targetThread, $false) | Out-Null
  }
  Start-Sleep -Milliseconds 220
}
$rect = New-Object WeChatWindowInfoWin32+RECT
[WeChatWindowInfoWin32]::GetWindowRect($handle, [ref]$rect) | Out-Null
"{0}`t{1}`t{2}`t{3}`t{4}" -f $handle.ToInt64(), $rect.Left, $rect.Top, ($rect.Right - $rect.Left), ($rect.Bottom - $rect.Top)
'''
    try:
        out = _win_powershell(
            script,
            _win_process_names(),
            "1" if activate else "0",
            _WIN_WINDOW_TITLE_HINT,
            _WIN_WINDOW_PROCESS_HINT,
            timeout=8.0,
        ).strip()
        handle, left, top, width, height = out.splitlines()[-1].split("\t")
        return WindowsWindowInfo(
            handle=int(handle),
            left=int(left),
            top=int(top),
            width=int(width),
            height=int(height),
        )
    except Exception as exc:
        raise WeChatForegroundError("Windows WeChat window was not found. Please open and log in to WeChat first.") from exc


def _win_wechat_rect(standardize: bool = False) -> tuple[int, int, int, int]:
    info = _win_wechat_window_info(activate=not _win_visual_strategy_enabled())
    if standardize:
        mode = _win_standardize_mode()
        if mode == "visual_maximize":
            _win_click_visual_maximize_button()
            info = _win_wechat_window_info(activate=not _win_visual_strategy_enabled())
        elif mode in {"maximize", "workarea"}:
            _win_wechat_rect_legacy_standardize(mode)
            info = _win_wechat_window_info(activate=not _win_visual_strategy_enabled())
    return info.left, info.top, info.width, info.height


def _win_wechat_rect_legacy_standardize(mode: str) -> None:
    info = _win_wechat_window_info(activate=True)
    if mode == "maximize":
        if os.getenv("WECHAT_WINDOWS_ALLOW_UNSAFE_RESIZE", "").strip() != "1":
            print(
                "[wechat_foreground] Windows API maximize is disabled because it can white-screen WeChat; "
                "set WECHAT_WINDOWS_ALLOW_UNSAFE_RESIZE=1 only for manual experiments.",
                flush=True,
            )
            return
        _win_maximize_window(info.handle)
        _sleep(0.3, 0.05)
        return
    if mode == "workarea":
        if os.getenv("WECHAT_WINDOWS_ALLOW_UNSAFE_RESIZE", "").strip() != "1":
            print(
                "[wechat_foreground] Windows MoveWindow workarea resize is disabled because it can white-screen WeChat; "
                "set WECHAT_WINDOWS_ALLOW_UNSAFE_RESIZE=1 only for manual experiments.",
                flush=True,
            )
            return
        script = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WeChatWorkAreaWin32 {
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool repaint);
}
"@
$handle = [IntPtr]::new([int64]$args[0])
$area = [System.Windows.Forms.Screen]::FromHandle($handle).WorkingArea
[WeChatWorkAreaWin32]::MoveWindow($handle, $area.Left, $area.Top, $area.Width, $area.Height, $true) | Out-Null
'''
        _win_powershell(script, str(info.handle), timeout=6.0)
        _sleep(0.3, 0.05)
        return
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WeChatWin32 {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr hWnd, int X, int Y, int nWidth, int nHeight, bool repaint);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
[WeChatWin32]::SetProcessDPIAware() | Out-Null
$names = $args[0] -split ','
$standardize = $args[1] -eq '1'
$standardizeMode = $args[2]
$script:proc = $null
$script:selectedHandle = [IntPtr]::Zero
$script:selectedArea = 0
foreach ($name in $names) {
  $procs = Get-Process -Name $name.Trim() -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending
  foreach ($candidate in $procs) {
    $callback = [WeChatWin32+EnumWindowsProc]{
      param([IntPtr]$hWnd, [IntPtr]$lParam)
      $windowPid = 0
      [WeChatWin32]::GetWindowThreadProcessId($hWnd, [ref]$windowPid) | Out-Null
      if ($windowPid -eq $candidate.Id -and [WeChatWin32]::IsWindowVisible($hWnd)) {
        $rect = New-Object WeChatWin32+RECT
        [WeChatWin32]::GetWindowRect($hWnd, [ref]$rect) | Out-Null
        $width = [Math]::Max(0, $rect.Right - $rect.Left)
        $height = [Math]::Max(0, $rect.Bottom - $rect.Top)
        $area = $width * $height
        if ($width -ge 560 -and $height -ge 500 -and $area -gt $script:selectedArea) {
          $script:selectedArea = $area
          $script:selectedHandle = $hWnd
          $script:proc = $candidate
        }
      }
      return $true
    }
    [WeChatWin32]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
  }
}
if (-not $proc) { throw "未找到 Windows 微信窗口，请先启动并登录微信。" }
$proc = $script:proc
$proc = $script:proc
$handle = $script:selectedHandle
if ($handle -eq [IntPtr]::Zero) { throw "找到微信进程但未找到顶层窗口，请先打开微信主窗口。" }
if ([WeChatWin32]::IsIconic($handle)) {
  [WeChatWin32]::ShowWindowAsync($handle, 9) | Out-Null
}
[WeChatWin32]::SetForegroundWindow($handle) | Out-Null
Start-Sleep -Milliseconds 250
if ($standardize) {
  if ($standardizeMode -eq "maximize") {
    [WeChatWin32]::ShowWindowAsync($handle, 3) | Out-Null
  } elseif ($standardizeMode -eq "workarea") {
    $area = [System.Windows.Forms.Screen]::FromHandle($handle).WorkingArea
    [WeChatWin32]::MoveWindow($handle, $area.Left, $area.Top, $area.Width, $area.Height, $true) | Out-Null
  }
  Start-Sleep -Milliseconds 250
}
$rect = New-Object WeChatWin32+RECT
[WeChatWin32]::GetWindowRect($handle, [ref]$rect) | Out-Null
"{0}`t{1}`t{2}`t{3}" -f $rect.Left, $rect.Top, ($rect.Right - $rect.Left), ($rect.Bottom - $rect.Top)
'''
    standardize_mode = mode if mode in {"maximize", "workarea"} else "none"
    out = _win_powershell(
        script,
        _win_process_names(),
        "1" if standardize_mode != "none" else "0",
        standardize_mode,
        timeout=8.0,
    ).strip()


def _win_activate_wechat() -> None:
    if _win_visual_strategy_enabled():
        _win_activate_wechat_visual_safe()
        return
    try:
        _win_wechat_window_info(activate=True)
        return
    except WeChatForegroundError as exc:
        _win_launch_wechat_if_needed()
        try:
            _win_wechat_window_info(activate=True)
            return
        except WeChatForegroundError:
            pass
        probe = _win_window_probe()
        if probe and sum(int(item.get("all_top_windows", 0)) for item in probe) == 0:
            raise WeChatForegroundError(
                "已找到 Windows 微信进程，但没有可接管的顶层窗口；"
                "请先手动打开并登录微信主窗口，再运行 "
                "`python -m src.infrastructure.wechat_foreground_collector --diagnose-windows` 预检。"
            ) from exc
        raise


def _win_activate_wechat_visual_safe() -> None:
    """Verify an already-frontmost WeChat window without Win32 focus APIs.

    Windows WeChat can white-screen when driven with SetForegroundWindow,
    AttachThreadInput, SetFocus, or synthetic window messages. The visual path
    therefore avoids taskbar/tray clicks too. The user or the surrounding
    desktop controller must put WeChat in front before this Python collector
    starts issuing real mouse clicks.
    """
    info: WindowsWindowInfo | None = None
    try:
        info = _win_wechat_window_info(activate=False)
    except WeChatForegroundError as exc:
        raise WeChatForegroundError(
            "Windows visual 模式没有找到已登录微信主窗口句柄；为避免误点任务栏/托盘，已停止自动接管。"
        ) from exc

    if info and _win_foreground_handle() == info.handle:
        _win_assert_renderable("windows", "after_window_restore_foreground")
        return

    if _win_window_info_looks_minimized(info):
        raise WeChatForegroundError(
            "Windows visual 模式检测到微信主窗口最小化；为避免 Win32 恢复触发微信白屏，已停止自动接管。"
        )
    foreground_process = _win_foreground_process_name()
    if not _win_process_name_is_wechat(foreground_process):
        raise WeChatForegroundError(
            "Windows visual 模式要求微信主窗口已经在前台；当前前台不是微信，已停止操作以避免误点其它窗口。"
        )
    _win_assert_renderable("windows", "after_window_restore_foreground")


def _win_foreground_handle() -> int:
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    return int(user32.GetForegroundWindow() or 0)


def _win_window_info_for_handle(handle: int) -> WindowsWindowInfo:
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    user32 = ctypes.windll.user32
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    if not user32.IsWindow(ctypes.c_void_p(handle)):
        raise WeChatForegroundError(f"Windows window handle no longer exists: {handle}")
    rect = RECT()
    if not user32.GetWindowRect(ctypes.c_void_p(handle), ctypes.byref(rect)):
        raise WeChatForegroundError(f"Windows failed to read window bounds: {handle}")
    return WindowsWindowInfo(
        handle=handle,
        left=int(rect.left),
        top=int(rect.top),
        width=int(rect.right - rect.left),
        height=int(rect.bottom - rect.top),
    )


def _win_window_process_name_for_handle(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(ctypes.c_void_p(handle), ctypes.byref(pid))
    if not pid.value:
        return ""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not process:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        if not kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).stem
    finally:
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(process)


def _win_window_title_for_handle(handle: int) -> str:
    import ctypes

    user32 = ctypes.windll.user32
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    length = max(0, int(user32.GetWindowTextLengthW(ctypes.c_void_p(handle))))
    buffer = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(ctypes.c_void_p(handle), buffer, len(buffer))
    return buffer.value.strip()


def _win_assert_wechat_window_handle(handle: int, purpose: str) -> None:
    process_name = _win_window_process_name_for_handle(handle)
    if not _win_process_name_is_wechat(process_name):
        raise WeChatForegroundError(
            f"Windows visual 模式拒绝操作非微信窗口({purpose}): handle={handle}, process={process_name or 'unknown'}"
        )


def _win_focus_known_window(handle: int) -> None:
    _win_assert_wechat_window_handle(handle, "focus")
    info = _win_window_info_for_handle(handle)
    if _win_window_info_looks_minimized(info):
        raise WeChatForegroundError(
            "Windows visual 模式不会用 Win32 API 恢复最小化窗口，避免触发微信白屏。"
        )
    _win_raise_window_without_focus_api(handle)
    _sleep(0.2, 0.05)
    if _win_foreground_handle() != handle and not _win_window_info_looks_minimized(info):
        title_x = info.left + min(max(info.width * 0.5, 80), max(info.width - 80, 80))
        _win_click_point(title_x, info.top + 18)
        _sleep(0.25, 0.05)
    if _win_foreground_handle() != handle:
        raise WeChatForegroundError(
            "Windows visual 模式无法聚焦已知微信窗口，已停止以避免误操作其它窗口。"
        )


def _win_close_known_window(handle: int) -> None:
    _win_assert_wechat_window_handle(handle, "close")
    info = _win_window_info_for_handle(handle)
    _win_focus_known_window(handle)
    # Click this specific window's title-bar close button instead of sending
    # Ctrl+W globally; this prevents closing Codex/VS Code when focus drifts.
    close_x = info.left + info.width - 18
    close_y = info.top + 18
    _win_click_point(close_x, close_y)


def _win_window_info_looks_minimized(info: WindowsWindowInfo) -> bool:
    return info.left <= -30000 or info.top <= -30000 or info.width < 300 or info.height < 120


def _win_restore_window_without_focus_api(handle: int) -> None:
    """Unsafe legacy helper; do not call from Windows visual mode."""
    import ctypes

    user32 = ctypes.windll.user32
    user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindowAsync.restype = ctypes.c_bool
    user32.ShowWindowAsync(ctypes.c_void_p(handle), 9)


def _win_restore_and_maximize_without_focus_api(handle: int) -> None:
    """Unsafe legacy helper; do not call from Windows visual mode."""
    import ctypes

    user32 = ctypes.windll.user32
    user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindowAsync.restype = ctypes.c_bool
    hwnd = ctypes.c_void_p(handle)
    user32.ShowWindowAsync(hwnd, 9)
    _sleep(0.25, 0.05)
    user32.ShowWindowAsync(hwnd, 3)


def _win_raise_window_without_focus_api(handle: int) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = ctypes.c_bool
    hwnd_top = ctypes.c_void_p(0)
    swp_nomove = 0x0002
    swp_nosize = 0x0001
    swp_noactivate = 0x0010
    swp_showwindow = 0x0040
    user32.SetWindowPos(
        ctypes.c_void_p(handle),
        hwnd_top,
        0,
        0,
        0,
        0,
        swp_nomove | swp_nosize | swp_noactivate | swp_showwindow,
    )


def _win_click_taskbar_wechat_icon() -> bool:
    detected = _win_detect_taskbar_wechat_icon_point_pil()
    if detected:
        _win_click_point(*detected)
        return True

    detected = _win_detect_taskbar_wechat_icon_point()
    if detected:
        _win_click_point(*detected)
        return True

    detected = _win_detect_taskbar_wechat_button_point()
    if detected:
        _win_click_point(*detected)
        return True

    point = os.getenv("WECHAT_WINDOWS_TASKBAR_POINT", "").strip()
    if point:
        try:
            x_raw, y_raw = [part.strip() for part in point.split(",", 1)]
            _win_click_point(float(x_raw), float(y_raw))
            return True
        except Exception:
            raise WeChatForegroundError(
                "WECHAT_WINDOWS_TASKBAR_POINT 格式应为 x,y，例如 890,1120。"
            )

    return False


def _win_detect_taskbar_wechat_icon_point_pil() -> tuple[int, int] | None:
    """Locate the taskbar WeChat icon from a real desktop screenshot."""
    try:
        from PIL import ImageGrab
    except Exception:
        return None

    try:
        try:
            image = ImageGrab.grab(all_screens=True)
        except TypeError:
            image = ImageGrab.grab()
    except Exception:
        return None

    if not image:
        return None

    width, height = image.size
    if width < 200 or height < 120:
        return None

    rgb = image.convert("RGB")
    y_start = max(0, height - max(96, int(height * 0.12)))
    y_end = height - 1
    step = 2
    x_scores: list[int] = [0] * width
    y_weighted: list[int] = [0] * width

    sampled = 0
    green_pixels = 0
    for x in range(0, width, step):
        count = 0
        y_sum = 0
        for y in range(y_start, y_end + 1, step):
            sampled += 1
            r, g, b = rgb.getpixel((x, y))
            # WeChat's taskbar icon is the most saturated green object in this band.
            if g >= 100 and r <= 160 and b <= 190 and (g - r) >= 10 and (g - b) >= 5:
                count += 1
                y_sum += y
        x_scores[x] = count
        y_weighted[x] = y_sum
        green_pixels += count

    if sampled <= 0 or green_pixels < 12:
        return None

    groups: list[tuple[int, int, int, int, int]] = []
    in_group = False
    start = 0
    total = 0
    weighted_x = 0
    weighted_y = 0
    # Do not scan the tray/status area: clicking the tray WeChat icon can hide
    # or toggle it instead of restoring the main WeChat window.
    max_usable_x = int(width * 0.75)
    min_column_score = 2

    for x in range(0, max_usable_x, step):
        score = x_scores[x]
        if score >= min_column_score:
            if not in_group:
                in_group = True
                start = x
                total = 0
                weighted_x = 0
                weighted_y = 0
            total += score
            weighted_x += x * score
            weighted_y += y_weighted[x]
        elif in_group:
            end = x
            if (end - start) >= 8 and total >= 10:
                groups.append((total, start, end, weighted_x, weighted_y))
            in_group = False

    if in_group:
        end = max_usable_x
        if (end - start) >= 8 and total >= 10:
            groups.append((total, start, end, weighted_x, weighted_y))

    if not groups:
        return None

    total, _start, _end, weighted_x, weighted_y = sorted(groups, reverse=True)[0]
    center_x = int(weighted_x / max(1, total))
    center_y = int(weighted_y / max(1, total))
    virtual_left, virtual_top, virtual_width, virtual_height = _win_virtual_screen_bounds()
    if (width, height) == (virtual_width, virtual_height):
        return virtual_left + center_x, virtual_top + center_y
    mapped_x = virtual_left + int(center_x * virtual_width / max(1, width))
    mapped_y = virtual_top + int(center_y * virtual_height / max(1, height))
    return mapped_x, mapped_y


def _win_detect_taskbar_wechat_icon_point() -> tuple[int, int] | None:
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
$graphics.Dispose()
$yStart = [Math]::Max(0, $bounds.Height - 96)
$yEnd = $bounds.Height - 1
$scores = New-Object int[] $bounds.Width
for ($x = 0; $x -lt $bounds.Width; $x += 2) {
  $count = 0
  for ($y = $yStart; $y -le $yEnd; $y += 2) {
    $c = $bitmap.GetPixel($x, $y)
    if ($c.G -ge 135 -and $c.R -le 95 -and $c.B -ge 45 -and $c.B -le 165 -and ($c.G - $c.R) -ge 55) {
      $count += 1
    }
  }
  $scores[$x] = $count
}
$bitmap.Dispose()
$groups = @()
$inGroup = $false
$start = 0
$sum = 0
$weighted = 0
$maxUsableX = [int]($bounds.Width * 0.92)
for ($x = 0; $x -lt $maxUsableX; $x += 2) {
  $score = $scores[$x]
  if ($score -ge 5) {
    if (-not $inGroup) {
      $inGroup = $true
      $start = $x
      $sum = 0
      $weighted = 0
    }
    $sum += $score
    $weighted += ($x * $score)
  } elseif ($inGroup) {
    $end = $x
    if (($end - $start) -ge 12 -and $sum -ge 20) {
      $center = [int]($weighted / [Math]::Max(1, $sum))
      $groups += [pscustomobject]@{Center=$center; Score=$sum; Width=($end - $start)}
    }
    $inGroup = $false
  }
}
if ($groups.Count -eq 0) { exit 0 }
$chosen = $groups | Sort-Object Score -Descending | Select-Object -First 1
$screenX = $bounds.Left + $chosen.Center
$screenY = $bounds.Top + $bounds.Height - 34
"{0}`t{1}" -f $screenX, $screenY
'''
    try:
        out = _win_powershell(script, timeout=8.0).strip()
    except Exception:
        return None
    if not out:
        return None
    try:
        x_raw, y_raw = out.splitlines()[-1].split("\t")
        return int(x_raw), int(y_raw)
    except ValueError:
        return None


def _win_detect_taskbar_wechat_button_point() -> tuple[int, int] | None:
    """Locate WeChat on the Windows taskbar through accessibility metadata."""
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$condition = [System.Windows.Automation.Condition]::TrueCondition
$nodes = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$candidates = @()
$wechatText = ([string][char]0x5fae) + ([string][char]0x4fe1)
foreach ($node in $nodes) {
  try {
    $name = $node.Current.Name
    if ([string]::IsNullOrWhiteSpace($name)) { continue }
    if (-not ($name.Contains($wechatText) -or $name -match "Weixin|WeChat")) { continue }
    $rect = $node.Current.BoundingRectangle
    if ($rect.Width -le 0 -or $rect.Height -le 0) { continue }
    $score = 0
    if ($rect.Top -ge ($bounds.Top + $bounds.Height - 120)) { $score += 1000 }
    if ($rect.Width -le 220 -and $rect.Height -le 100) { $score += 200 }
    if ($name -eq $wechatText -or $name -match "^Weixin$|^WeChat$") { $score += 100 }
    $candidates += [pscustomobject]@{
      Score=$score
      X=[int]($rect.Left + ($rect.Width / 2))
      Y=[int]($rect.Top + ($rect.Height / 2))
      Name=$name
    }
  } catch {}
}
if ($candidates.Count -eq 0) { exit 0 }
$chosen = $candidates | Sort-Object Score -Descending | Select-Object -First 1
"{0}`t{1}`t{2}" -f $chosen.X, $chosen.Y, $chosen.Name
'''
    try:
        out = _win_powershell(script, timeout=10.0).strip()
    except Exception:
        return None
    if not out:
        return None
    try:
        x_raw, y_raw, *_ = out.splitlines()[-1].split("\t")
        return int(x_raw), int(y_raw)
    except ValueError:
        return None


def _win_open_search_deeplink(account_name: str, force: bool = False) -> None:
    if not force and not _win_search_deeplink_enabled():
        print(
            "[wechat_foreground] Windows search deeplink skipped; "
            "set WECHAT_WINDOWS_ENABLE_DEEPLINK=1 after allowing foreground control.",
            flush=True,
        )
        return
    query = quote(account_name, safe="")
    uri = f"weixin://resourceid/Search/app.html?isHomePage=0&lang=zh_CN&query={query}&scene=85&type=51"
    try:
        _win_powershell("Start-Process -FilePath $args[0]", uri, timeout=6.0)
    except Exception as exc:
        print(f"[wechat_foreground] Windows 搜索深链唤起失败: {exc}", flush=True)


def _win_search_deeplink_enabled(user_confirmed_foreground: bool = False) -> bool:
    return user_confirmed_foreground or os.getenv("WECHAT_WINDOWS_ENABLE_DEEPLINK", "").strip() == "1"


def _win_foreground_clicks_enabled() -> bool:
    strategy = os.getenv("WECHAT_WINDOWS_FOREGROUND_STRATEGY", "").strip().lower()
    return strategy == "visual" or os.getenv("WECHAT_WINDOWS_ALLOW_FOREGROUND_CLICKS", "").strip() == "1"


def _win_visual_strategy_enabled() -> bool:
    return os.getenv("WECHAT_WINDOWS_FOREGROUND_STRATEGY", "").strip().lower() == "visual"


def _win_prepare_visual_step(stage: str) -> None:
    if not _win_visual_strategy_enabled():
        return
    _win_wechat_window_info(activate=False)
    if os.getenv("WECHAT_WINDOWS_VISUAL_MAXIMIZE_EACH_STEP", "0").strip() == "1":
        _win_ensure_visual_maximized_once(stage)
    _win_assert_renderable("windows", f"visual_{stage}")


def _win_set_window_title_hint(title_hint: str) -> None:
    global _WIN_WINDOW_TITLE_HINT
    _WIN_WINDOW_TITLE_HINT = title_hint.strip()


def _win_set_window_process_hint(process_hint: str) -> None:
    global _WIN_WINDOW_PROCESS_HINT
    _WIN_WINDOW_PROCESS_HINT = process_hint.strip()


def _win_reset_visual_session() -> None:
    global _WIN_VISUAL_MAXIMIZE_ATTEMPTED
    _WIN_VISUAL_MAXIMIZE_ATTEMPTED = False


def _win_ensure_initial_visual_maximized(account_name: str = "", verify_renderable: bool = True) -> None:
    """The visual path starts only after the existing WeChat window is maximized."""
    if not _win_visual_strategy_enabled():
        return
    info = _win_wechat_window_info(activate=False)
    _win_assert_wechat_window_handle(info.handle, "initial visual fullscreen check")
    if _win_window_info_looks_minimized(info):
        raise WeChatForegroundError(
            "Windows visual mode found WeChat minimized before search; refusing to continue."
        )
    if not _win_window_looks_maximized(info):
        _win_ensure_visual_maximized_once("initial")
        _sleep(_win_env_float("WECHAT_WINDOWS_AFTER_INITIAL_MAXIMIZE_WAIT", 0.35), 0.08)
    refreshed = _win_wechat_window_info(activate=False)
    if not _win_window_looks_maximized(refreshed):
        raise WeChatForegroundError(
            "Windows visual mode could not maximize WeChat before search; refusing to continue."
        )
    _win_focus_known_window(refreshed.handle)
    if verify_renderable:
        _win_assert_renderable(account_name or "windows", "visual_initial_maximized")


def _win_ensure_foreground_visual_maximized(account_name: str = "", verify_renderable: bool = True) -> None:
    handle = _win_foreground_handle()
    if not handle:
        raise WeChatForegroundError("Windows visual mode found no foreground window before search.")
    _win_assert_wechat_window_handle(handle, "foreground visual fullscreen check")
    info = _win_window_info_for_handle(handle)
    if _win_window_info_looks_minimized(info):
        raise WeChatForegroundError(
            "Windows visual mode found foreground WeChat minimized before search; refusing to continue."
        )
    if info.width < 900 or info.height < 650:
        raise WeChatForegroundError(
            "Windows visual mode found a small foreground WeChat popup before search; refusing to maximize it."
        )
    if not _win_window_looks_maximized(info):
        _win_click_visual_maximize_button_for_info(info)
        _sleep(_win_env_float("WECHAT_WINDOWS_AFTER_INITIAL_MAXIMIZE_WAIT", 0.35), 0.08)
    refreshed = _win_window_info_for_handle(handle)
    if not _win_window_looks_maximized(refreshed):
        raise WeChatForegroundError(
            "Windows visual mode could not maximize the foreground WeChat window before search."
        )
    _win_focus_known_window(handle)
    if verify_renderable:
        _win_assert_renderable(account_name or "windows", "visual_foreground_maximized")


def _win_ensure_visual_maximized_once(stage: str = "") -> None:
    """Avoid toggling a maximized WeChat window back to restored size."""
    global _WIN_VISUAL_MAXIMIZE_ATTEMPTED
    info = _win_wechat_window_info(activate=False)
    if _win_window_looks_maximized(info):
        return
    if _WIN_VISUAL_MAXIMIZE_ATTEMPTED:
        print(
            f"[wechat_foreground] Windows visual maximize already attempted; "
            f"skip repeated title-bar click at {stage} to avoid restoring WeChat.",
            flush=True,
        )
        return
    _WIN_VISUAL_MAXIMIZE_ATTEMPTED = True
    _win_click_visual_maximize_button()


def _win_ensure_visual_maximized_for_search() -> None:
    """Search coordinates are only stable after WeChat is maximized."""
    if not _win_visual_strategy_enabled():
        return
    info = _win_wechat_window_info(activate=False)
    if _win_window_looks_maximized(info):
        return
    if _win_window_info_looks_minimized(info):
        raise WeChatForegroundError("Windows 微信搜索前窗口处于最小化，已停止以避免 API 恢复触发白屏。")
    _win_click_visual_maximize_button()
    _sleep(0.6, 0.12)
    refreshed = _win_wechat_window_info(activate=False)
    if not _win_window_looks_maximized(refreshed):
        raise WeChatForegroundError("Windows 微信搜索前未能最大化，停止操作以避免坐标漂移。")


def _win_standardize_wechat_window() -> None:
    if _win_visual_strategy_enabled():
        return
    if os.getenv("WECHAT_WINDOWS_SKIP_STANDARDIZE", "").strip() == "1":
        _win_activate_wechat()
        return
    try:
        mode = _win_standardize_mode()
        if mode == "none":
            _win_activate_wechat()
        elif mode == "visual_maximize":
            if os.getenv("WECHAT_WINDOWS_ALLOW_VISUAL_MAXIMIZE", "").strip() == "1":
                _win_click_visual_maximize_button()
            else:
                print(
                    "[wechat_foreground] Windows visual maximize is disabled by default to avoid WeChat white screens; "
                    "continuing with window-relative search.",
                    flush=True,
                )
                _win_activate_wechat()
        else:
            _win_wechat_rect(standardize=True)
    except WeChatForegroundError:
        _win_activate_wechat()


def _win_standardize_mode() -> str:
    mode = os.getenv("WECHAT_WINDOWS_STANDARDIZE_MODE", "none").strip().lower()
    aliases = {
        "visual": "visual_maximize",
        "maximize_button": "visual_maximize",
        "button": "visual_maximize",
    }
    mode = aliases.get(mode, mode)
    if mode not in {"visual_maximize", "maximize", "workarea", "none"}:
        mode = "none"
    return mode


def _win_click_visual_maximize_button() -> None:
    """Maximize by clicking the visible title-bar button, avoiding white-screen MoveWindow paths."""
    info = _win_wechat_window_info(activate=False)
    if _win_window_looks_maximized(info):
        return
    _win_click_visual_maximize_button_for_info(info)


def _win_click_visual_maximize_button_for_info(info: WindowsWindowInfo) -> None:
    if _win_window_looks_maximized(info):
        return

    # WeChat uses a normal top-right title-bar cluster. The maximize button is
    # the middle of minimize/maximize/close and stays stable after activation.
    right = info.left + info.width
    x = right - _win_env_int("WECHAT_WINDOWS_MAXIMIZE_BUTTON_RIGHT_OFFSET", 78)
    y = info.top + _win_env_int("WECHAT_WINDOWS_MAXIMIZE_BUTTON_TOP_OFFSET", 18)
    _win_click_point(x, y)
    _sleep(0.55, 0.1)

    refreshed = _win_wechat_window_info(activate=False)
    if not _win_window_looks_maximized(refreshed):
        print(
            "[wechat_foreground] Windows visual maximize did not change the WeChat window; "
            "skip API maximize to avoid triggering a WeChat white screen.",
            flush=True,
        )
    _win_assert_renderable("windows", "after_visual_maximize")


def _win_maximize_window(handle: int) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindowAsync.restype = ctypes.c_bool
    hwnd = ctypes.c_void_p(handle)
    user32.ShowWindowAsync(hwnd, 3)
    _win_force_foreground_handle(handle)


def _win_force_foreground_handle(handle: int) -> bool:
    """Best-effort foreground activation that survives VSCode/terminal launch context."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = ctypes.c_void_p(handle)

    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetActiveWindow.argtypes = [ctypes.c_void_p]
    user32.SetActiveWindow.restype = ctypes.c_void_p
    user32.SetFocus.argtypes = [ctypes.c_void_p]
    user32.SetFocus.restype = ctypes.c_void_p

    user32.ShowWindowAsync(hwnd, 5)
    if user32.IsIconic(hwnd):
        user32.ShowWindowAsync(hwnd, 9)
        time.sleep(0.12)

    current_thread = kernel32.GetCurrentThreadId()
    target_pid = wintypes.DWORD(0)
    target_thread = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
    foreground = user32.GetForegroundWindow()
    foreground_thread = 0
    if foreground:
        foreground_pid = wintypes.DWORD(0)
        foreground_thread = user32.GetWindowThreadProcessId(
            ctypes.c_void_p(foreground), ctypes.byref(foreground_pid)
        )

    attached_target = False
    attached_foreground = False
    try:
        if target_thread and target_thread != current_thread:
            attached_target = bool(user32.AttachThreadInput(current_thread, target_thread, True))
        if foreground_thread and foreground_thread not in (current_thread, target_thread):
            attached_foreground = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetActiveWindow(hwnd)
        user32.SetFocus(hwnd)
        time.sleep(0.12)
    finally:
        if attached_foreground:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
        if attached_target:
            user32.AttachThreadInput(current_thread, target_thread, False)

    return user32.GetForegroundWindow() == handle


def _win_window_is_maximized(handle: int) -> bool:
    import ctypes

    user32 = ctypes.windll.user32
    user32.IsZoomed.argtypes = [ctypes.c_void_p]
    user32.IsZoomed.restype = ctypes.c_bool
    return bool(user32.IsZoomed(ctypes.c_void_p(handle)))


def _win_window_looks_maximized(info: WindowsWindowInfo) -> bool:
    if _win_window_is_maximized(info.handle):
        return True
    try:
        work_left, work_top, work_width, work_height = _win_window_work_area(info.handle)
    except Exception:
        return False
    # On mixed-DPI multi-monitor setups, GetWindowRect and monitor work-area
    # coordinates can be reported in different coordinate spaces. Size is more
    # reliable than the absolute top-left position for detecting maximized
    # WeChat, and avoids clicking the title-bar button again, which would
    # restore the window to its previous size.
    width_ok = info.width >= int(work_width * 0.95)
    height_ok = info.height >= int(work_height * 0.95)
    return width_ok and height_ok


def _win_window_work_area(handle: int) -> tuple[int, int, int, int]:
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    monitor = user32.MonitorFromWindow(ctypes.c_void_p(handle), 2)
    if not monitor:
        raise WeChatForegroundError("Windows monitor for WeChat was not found.")
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise WeChatForegroundError("Windows monitor work area was not available.")
    rect = info.rcWork
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _win_screen_size() -> tuple[int, int]:
    left, top, width, height = _win_virtual_screen_bounds()
    return width, height


def _win_virtual_screen_bounds() -> tuple[int, int, int, int]:
    import ctypes

    user32 = ctypes.windll.user32
    return (
        int(user32.GetSystemMetrics(76)),
        int(user32.GetSystemMetrics(77)),
        int(user32.GetSystemMetrics(78)),
        int(user32.GetSystemMetrics(79)),
    )


def _win_click_norm(norm_x: float, norm_y_from_bottom: float) -> None:
    left, top, width, height = _win_virtual_screen_bounds()
    _win_click_point(left + norm_x * width, top + (1.0 - norm_y_from_bottom) * height)


def _win_click_window(rel_x: float, rel_y_from_top: float) -> None:
    left, top, width, height = _win_wechat_rect(standardize=False)
    _win_click_point(left + rel_x * width, top + rel_y_from_top * height)


def _win_click_client(handle: int, x: int, y: int) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    point = wintypes.POINT(int(x), int(y))
    if not user32.ClientToScreen(ctypes.c_void_p(handle), ctypes.byref(point)):
        raise WeChatForegroundError("Windows failed to convert WeChat client coordinates to screen coordinates.")
    _win_click_point(point.x, point.y)


def _win_click_search_box_by_client_message() -> bool:
    info = _win_wechat_window_info(activate=False)
    points = _win_client_point_sequence(
        "WECHAT_WINDOWS_SEARCH_CLIENT_POINTS",
        "200,55;180,55;240,55",
    )
    for x, y in points:
        _win_click_client(info.handle, int(x), int(y))
        _sleep(0.08, 0.02)
    return True


def _win_click_point(x: float, y: float) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def _win_key(vk: int, key_up: bool = False) -> None:
    import ctypes

    ctypes.windll.user32.keybd_event(vk, 0, 0x0002 if key_up else 0, 0)


def _win_hotkey(*vks: int) -> None:
    for vk in vks:
        _win_key(vk)
        time.sleep(0.03)
    for vk in reversed(vks):
        _win_key(vk, key_up=True)
        time.sleep(0.03)


def _win_press(vk: int) -> None:
    _win_key(vk)
    time.sleep(0.04)
    _win_key(vk, key_up=True)


def _win_paste_text(text: str) -> None:
    if not _win_visual_strategy_enabled():
        try:
            info = _win_wechat_window_info(activate=True)
            _win_force_foreground_handle(info.handle)
        except WeChatForegroundError:
            pass
    _win_set_clipboard(text)
    _sleep(0.15, 0.03)
    _win_hotkey(0x11, 0x41)
    _sleep(0.1, 0.02)
    _win_hotkey(0x11, 0x56)


def _win_set_clipboard(text: str) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    data = (text + "\0").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise WeChatForegroundError("failed to allocate Windows clipboard memory")
    locked = kernel32.GlobalLock(handle)
    if not locked:
        kernel32.GlobalFree(handle)
        raise WeChatForegroundError("failed to lock Windows clipboard memory")
    ctypes.memmove(locked, data, len(data))
    kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise WeChatForegroundError("failed to open Windows clipboard")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise WeChatForegroundError("failed to set Windows clipboard text")
        handle = None
    finally:
        user32.CloseClipboard()


def _win_clipboard() -> str:
    script = "try { Get-Clipboard -Raw } catch { '' }"
    return _win_powershell(script, timeout=4.0)


def _win_texts() -> list[OCRText]:
    script = r'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WeChatTextWin32 {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
$names = $args[0] -split ','
$script:proc = $null
$script:handle = [IntPtr]::Zero
$script:selectedArea = 0
foreach ($name in $names) {
  $procs = Get-Process -Name $name.Trim() -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending
  foreach ($candidate in $procs) {
    $callback = [WeChatTextWin32+EnumWindowsProc]{
      param([IntPtr]$hWnd, [IntPtr]$lParam)
      $windowPid = 0
      [WeChatTextWin32]::GetWindowThreadProcessId($hWnd, [ref]$windowPid) | Out-Null
      if ($windowPid -eq $candidate.Id -and -not [WeChatTextWin32]::IsIconic($hWnd)) {
        $rect = New-Object WeChatTextWin32+RECT
        [WeChatTextWin32]::GetWindowRect($hWnd, [ref]$rect) | Out-Null
        $width = [Math]::Max(0, $rect.Right - $rect.Left)
        $height = [Math]::Max(0, $rect.Bottom - $rect.Top)
        $area = $width * $height
        if ($width -ge 560 -and $height -ge 500 -and $area -gt $script:selectedArea) {
          $script:selectedArea = $area
          $script:handle = $hWnd
          $script:proc = $candidate
        }
      }
      return $true
    }
    [WeChatTextWin32]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
  }
}
$proc = $script:proc
$handle = $script:handle
if (-not $proc) { exit 0 }
if ($handle -eq [IntPtr]::Zero) { exit 0 }
$root = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
if (-not $root) { exit 0 }
$condition = [System.Windows.Automation.Condition]::TrueCondition
$nodes = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
foreach ($node in $nodes) {
  try {
    $name = $node.Current.Name
    $rect = $node.Current.BoundingRectangle
    if ([string]::IsNullOrWhiteSpace($name)) { continue }
    if ($rect.Width -le 0 -or $rect.Height -le 0) { continue }
    $safe = $name -replace "`t", " " -replace "`r|`n", " "
    "{0}`t{1}`t{2}`t{3}`t{4}" -f [int]$rect.Left, [int]$rect.Top, [int]$rect.Width, [int]$rect.Height, $safe
  } catch {}
}
'''
    virtual_left, virtual_top, screen_w, screen_h = _win_virtual_screen_bounds()
    texts: list[OCRText] = []
    try:
        out = _win_powershell(script, _win_process_names(), timeout=12.0)
    except Exception:
        return texts
    for line in out.splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        try:
            left, top, width, height = (float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3]))
        except ValueError:
            continue
        texts.append(
            OCRText(
                text=parts[4].strip(),
                x=(left - virtual_left) / screen_w,
                y=1.0 - (((top - virtual_top) + height) / screen_h),
                w=width / screen_w,
                h=height / screen_h,
            )
        )
    return texts


def _win_search_account(account_name: str) -> None:
    _win_set_window_title_hint("")
    _win_set_window_process_hint("Weixin")
    if _win_visual_strategy_enabled():
        _win_open_main_chat_tab_visual()
        _win_click_search_box_by_client_message()
        _sleep(0.12, 0.03)
        _win_paste_text(account_name)
        return
    if not _win_visual_strategy_enabled():
        _win_press(0x1B)
        _sleep(0.15, 0.03)
    search = _win_find_top_left_search_placeholder()
    if search:
        _win_click_norm(search.cx, search.cy)
    else:
        _win_click_search_box_by_client_message()
    _sleep(0.25, 0.05)
    _win_paste_text(account_name)


def _win_open_account_result(account_name: str) -> bool:
    if _win_visual_strategy_enabled():
        attempts: list[tuple[str, tuple[float, float]]] = []
        result = _win_find_account_result(account_name)
        if result:
            attempts.append(("norm", (result.cx, result.cy)))
        attempts.extend(("client", (float(x), float(y))) for x, y in _win_search_result_client_points())

        seen: set[tuple[str, int, int]] = set()
        for index, (kind, point) in enumerate(attempts):
            key = (kind, int(point[0] * 10000) if kind == "norm" else int(point[0]), int(point[1] * 10000) if kind == "norm" else int(point[1]))
            if key in seen:
                continue
            seen.add(key)
            if index:
                _win_search_account(account_name)
                _sleep(_win_env_float("WECHAT_WINDOWS_AFTER_SEARCH_WAIT", 0.65), 0.1)
            if kind == "norm":
                _win_click_norm(point[0], point[1])
            else:
                info = _win_wechat_window_info(activate=False)
                _win_click_client(info.handle, int(point[0]), int(point[1]))
            _sleep(0.85, 0.15)
            if _win_opened_expected_account_result(account_name):
                return True
            _win_recover_after_wrong_account_result()
        return False
    for _ in range(4):
        result = _win_find_account_result(account_name)
        if result:
            _win_click_norm(result.cx, result.cy)
            if _win_visual_strategy_enabled():
                return True
            _sleep(0.2, 0.05)
            _win_click_norm(result.cx, result.cy)
            return True
        _sleep(0.5, 0.1)

    # Windows WeChat exposes the global search results as a narrow left panel.
    # In mixed-DPI multi-monitor setups, full-window relative screen clicks can
    # drift badly. Client coordinates are stable here because the search box was
    # also focused through the same message path.
    _win_click_search_result_by_client_message()
    return True


def _win_click_search_result_by_client_message() -> None:
    info = _win_wechat_window_info(activate=False)
    points = _win_search_result_client_points()
    if points:
        x, y = points[0]
        _win_click_client(info.handle, int(x), int(y))
        _sleep(0.18, 0.04)
    _sleep(0.8, 0.15)


def _win_search_result_client_points() -> list[tuple[int, int]]:
    return _win_client_point_sequence(
        "WECHAT_WINDOWS_RESULT_CLIENT_POINTS",
        "185,238;185,405;205,405;165,405;185,545;205,545;165,545",
    )


def _win_opened_expected_account_result(account_name: str) -> bool:
    handle = _win_foreground_handle()
    if not handle:
        return False
    try:
        _win_assert_wechat_window_handle(handle, "account result validation")
    except WeChatForegroundError:
        return False
    main_handle = 0
    try:
        main_handle = _win_choose_existing_wechat_window_handle()
    except Exception:
        main_handle = 0
    if main_handle and handle == main_handle:
        return False

    title = _win_window_title_for_handle(handle)
    visible = " | ".join(item.text for item in _win_texts())
    haystack = f"{title} | {visible}".lower()
    query = account_name.lower()
    bad_markers = ("搜一搜", "搜索网络结果", "搜索聊天记录", "聊天记录", "Search chat history")
    if any(marker.lower() in haystack for marker in bad_markers):
        return False
    return query in haystack or "公众号" in haystack


def _win_recover_after_wrong_account_result() -> None:
    handle = _win_foreground_handle()
    main_handle = 0
    try:
        main_handle = _win_choose_existing_wechat_window_handle()
    except Exception:
        main_handle = 0
    if handle and main_handle and handle != main_handle:
        try:
            _win_close_known_window(handle)
            _sleep(0.45, 0.08)
        except WeChatForegroundError:
            pass
    if main_handle:
        try:
            _win_focus_known_window(main_handle)
        except WeChatForegroundError:
            pass
    _win_press(0x1B)
    _sleep(0.2, 0.05)


def _win_looks_like_chat_history_search_popup() -> bool:
    visible = " | ".join(item.text for item in _win_texts())
    return any(marker in visible for marker in ("搜索聊天记录", "搜尋聊天記錄", "Search chat history"))


def _win_open_main_chat_tab_visual() -> None:
    info = _win_wechat_window_info(activate=False)
    if info.width < 900:
        return
    points = _win_client_point_sequence(
        "WECHAT_WINDOWS_CHAT_TAB_CLIENT_POINTS",
        "36,96;36,72",
    )
    for x, y in points[:1]:
        _win_click_client(info.handle, int(x), int(y))
        _sleep(0.12, 0.03)


def _win_mark_account_window(account_name: str) -> None:
    _win_set_window_title_hint(account_name)
    _win_set_window_process_hint("Weixin")


def _win_ensure_full_account_home(account_name: str, soft: bool = False) -> None:
    if _win_looks_like_full_account_home(account_name):
        return
    if _win_visual_strategy_enabled():
        _win_open_full_account_home_visual(account_name)
        if not _win_looks_like_full_account_home(account_name):
            if soft:
                return
            raise WeChatForegroundError(f"Windows 未确认进入公众号完整主页: {account_name}")
        return
    for x, y in _win_point_sequence("WECHAT_WINDOWS_HOME_POINTS", "0.955,0.075;0.930,0.115;0.500,0.180"):
        _win_click_window(x, y)
        _sleep(1.3, 0.25)
        if _win_looks_like_full_account_home(account_name):
            return
    if not soft:
        print("[wechat_foreground] Windows 未确认进入完整主页，继续使用当前页面尝试采集。", flush=True)


def _win_open_full_account_home_visual(account_name: str, soft: bool = False) -> None:
    handle = _win_foreground_handle() or _win_wechat_window_info(activate=False).handle
    _win_assert_wechat_window_handle(handle, "account profile")
    info = _win_window_info_for_handle(handle)
    if _win_window_info_looks_minimized(info):
        raise WeChatForegroundError("Windows 公众号浮层最小化，无法进入完整主页。")

    points = _win_client_point_sequence(
        "WECHAT_WINDOWS_ACCOUNT_PROFILE_CLIENT_POINTS",
        f"{max(24, info.width - _win_env_int('WECHAT_WINDOWS_ACCOUNT_PROFILE_RIGHT_OFFSET', 38))},"
        f"{_win_env_int('WECHAT_WINDOWS_ACCOUNT_PROFILE_TOP_OFFSET', 64)};"
        f"{max(24, info.width - 52)},{_win_env_int('WECHAT_WINDOWS_ACCOUNT_PROFILE_TOP_OFFSET', 64)}",
    )
    for x, y in points:
        _win_click_client(handle, int(x), int(y))
        _sleep(_win_env_float("WECHAT_WINDOWS_AFTER_PROFILE_CLICK_WAIT", 0.9), 0.18)
        if _win_looks_like_full_account_home(account_name):
            return
        current = _win_window_info_for_handle(_win_foreground_handle() or handle)
        if current.width >= info.width + 80 or current.height >= info.height + 120:
            return
    if not soft:
        print("[wechat_foreground] Windows 未确认进入公众号完整主页，继续使用当前页面尝试采集。", flush=True)


def _win_looks_like_full_account_home(account_name: str = "") -> bool:
    texts = _win_texts()
    visible = " | ".join(item.text for item in texts)
    tab_count = sum(1 for item in texts if item.text.strip() in {"全部", "贴图", "文章", "视频号"})
    has_profile = any(
        marker in visible
        for marker in ("已关注", "发消息", "篇原创内容", "个朋友关注", account_name)
        if marker
    )
    return tab_count >= 2 or has_profile


def _win_find_next_article_on_account_page(date_labels: list[str], visited_titles: set[str]) -> OCRText | None:
    texts = _win_texts()
    separators = [
        item for item in texts
        if 0.22 < item.x < 0.90
        and (
            item.text in {"今天", "昨天"}
            or item.text.startswith("星期")
            or ("年" in item.text and "月" in item.text and "日" in item.text)
        )
    ]
    target_separators = [item for item in separators if any(label in item.text for label in date_labels)]
    sections: list[tuple[float, float]] = []
    for anchor in sorted(target_separators, key=lambda item: -item.y):
        next_separators = [item for item in separators if item.y < anchor.y]
        lower_bound = max((item.y for item in next_separators), default=0.0)
        sections.append((anchor.y, lower_bound))
    if not sections:
        sections.append((0.72, 0.10))

    excluded = (
        "阅读", "赞", "朋友看过", "已关注", "发消息", "全部", "贴图", "文章", "视频号",
        "公众号", "搜索", "复制链接", "发送给朋友", "收藏", "投诉", "微信",
    )
    candidates = [
        item for item in texts
        if 0.25 < item.x < 0.85
        and any(item.y < upper and item.y > lower for upper, lower in sections)
        and len(item.text) >= 6
        and re.search(r"[\u4e00-\u9fff]", item.text)
        and "/" not in item.text
        and not any(word in item.text for word in excluded)
        and item.text not in visited_titles
    ]
    return sorted(candidates, key=lambda item: -item.y)[0] if candidates else None


def _win_open_article_by_index(index: int) -> bool:
    if _win_visual_strategy_enabled():
        points = _win_client_point_sequence(
            "WECHAT_WINDOWS_ARTICLE_CLIENT_POINTS",
            "300,180;300,430;300,555;300,680;300,805",
        )
        if index >= len(points):
            return False
        handle = _win_foreground_handle() or _win_wechat_window_info(activate=False).handle
        x, y = points[index]
        _win_click_client(handle, int(x), int(y))
        return True

    points = _win_point_sequence("WECHAT_WINDOWS_ARTICLE_POINTS", "0.500,0.360;0.500,0.500;0.500,0.640")
    if index >= len(points):
        return False
    x, y = points[index]
    _win_click_window(x, y)
    return True


def _win_copy_visible_article_url(article_handle: int | None = None) -> str:
    _win_set_clipboard("")
    if _win_visual_strategy_enabled():
        try:
            if article_handle:
                _win_focus_known_window(article_handle)
                bounds = _win_window_info_for_handle(article_handle)
                base_left, base_top, base_width = bounds.left, bounds.top, bounds.width
            else:
                _win_wechat_window_info(activate=False)
                base_left, base_top, base_width, _screen_height = _win_virtual_screen_bounds()
            menu_x = base_left + base_width - _win_env_int("WECHAT_WINDOWS_ARTICLE_MENU_RIGHT_OFFSET", 117)
            menu_y = base_top + _win_env_int("WECHAT_WINDOWS_ARTICLE_MENU_TOP_OFFSET", 20)
            _win_click_point(menu_x, menu_y)
            _sleep(0.8, 0.15)
            copy_x = base_left + base_width - _win_env_int("WECHAT_WINDOWS_ARTICLE_COPY_RIGHT_OFFSET", 315)
            copy_y = base_top + _win_env_int("WECHAT_WINDOWS_ARTICLE_COPY_TOP_OFFSET", 93)
            _win_click_point(copy_x, copy_y)
            _sleep(0.8, 0.15)
            text = _win_clipboard().strip()
            if "mp.weixin.qq.com" in text:
                return text
            return text
        except WeChatForegroundError:
            raise
        except Exception as exc:
            print(f"[wechat_foreground] Windows visual copy-link fallback failed: {exc}", flush=True)
            return _win_clipboard().strip()

    for x, y in _win_point_sequence("WECHAT_WINDOWS_MORE_POINTS", "0.965,0.070;0.945,0.095;0.925,0.070"):
        more = _win_find_menu_or_button(("更多", "...", "···", "…"))
        if more:
            _win_click_norm(more.cx, more.cy)
        else:
            _win_click_window(x, y)
        _sleep(0.8, 0.15)
        copy_item = _win_find_menu_or_button(("复制链接", "拷贝链接", "复制 link", "Copy link"))
        if copy_item:
            _win_click_norm(copy_item.cx, copy_item.cy)
            _sleep(0.9, 0.15)
            return _win_clipboard().strip()
        for cx, cy in _win_point_sequence("WECHAT_WINDOWS_COPY_POINTS", "0.890,0.185;0.870,0.220;0.815,0.175"):
            _win_click_window(cx, cy)
            _sleep(0.8, 0.12)
            text = _win_clipboard().strip()
            if "mp.weixin.qq.com" in text:
                return text
    return _win_clipboard().strip()


def _win_return_to_account_page() -> None:
    for attempt in range(4):
        if _win_looks_like_full_account_home():
            return
        if attempt < 2:
            _win_hotkey(0x11, 0x57)
        else:
            _win_hotkey(0x12, 0x25)
        _sleep(0.9, 0.2)


def _win_cleanup_after_account() -> None:
    if os.getenv("WECHAT_FOREGROUND_KEEP_ACCOUNT_WINDOW", "").strip() == "1":
        return
    for _ in range(2):
        _win_hotkey(0x11, 0x57)
        _sleep(0.5, 0.12)


def _win_debug_capture(account_name: str, stage: str) -> Path | None:
    if os.getenv("WECHAT_FOREGROUND_DEBUG", "1").strip() == "0":
        return None
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in account_name)
    path = DEBUG_DIR / f"{int(time.time() * 1000)}_{safe_name}_{stage}.png"
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
try {
  Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WeChatCaptureWin32 {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out int processId);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hwnd, IntPtr hdcBlt, uint nFlags);
  public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
[WeChatCaptureWin32]::SetProcessDPIAware() | Out-Null
} catch {}
$script:targetHandle = [IntPtr]::Zero
$script:targetArea = 0
try {
  $names = $args[1] -split ','
  foreach ($name in $names) {
    $procs = Get-Process -Name $name.Trim() -ErrorAction SilentlyContinue | Sort-Object StartTime -Descending
    foreach ($candidate in $procs) {
      $callback = [WeChatCaptureWin32+EnumWindowsProc]{
        param([IntPtr]$hWnd, [IntPtr]$lParam)
        $windowPid = 0
        [WeChatCaptureWin32]::GetWindowThreadProcessId($hWnd, [ref]$windowPid) | Out-Null
        if ($windowPid -eq $candidate.Id -and -not [WeChatCaptureWin32]::IsIconic($hWnd)) {
          $rect = New-Object WeChatCaptureWin32+RECT
          [WeChatCaptureWin32]::GetWindowRect($hWnd, [ref]$rect) | Out-Null
          $width = [Math]::Max(0, $rect.Right - $rect.Left)
          $height = [Math]::Max(0, $rect.Bottom - $rect.Top)
          $area = $width * $height
          if ($width -ge 700 -and $height -ge 500 -and $area -gt $script:targetArea) {
            $script:targetArea = $area
            $script:targetHandle = $hWnd
          }
        }
        return $true
      }
      [WeChatCaptureWin32]::EnumWindows($callback, [IntPtr]::Zero) | Out-Null
    }
  }
} catch {}
$targetHandle = $script:targetHandle
if ($targetHandle -ne [IntPtr]::Zero) {
  $bounds = [System.Windows.Forms.Screen]::FromHandle($targetHandle).Bounds
  $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
  $bitmap.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png)
  $graphics.Dispose()
  $bitmap.Dispose()
} else {
  $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
  $bitmap.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png)
  $graphics.Dispose()
  $bitmap.Dispose()
}
'''
    try:
        _win_powershell(script, str(path), _win_process_names(), timeout=6.0)
    except Exception:
        return None
    return path


def _win_assert_renderable(account_name: str, stage: str) -> None:
    if _win_visual_strategy_enabled():
        foreground_process = _win_foreground_process_name()
        if not _win_process_name_is_wechat(foreground_process):
            raise WeChatForegroundError(
                "Windows visual 模式检测到当前前台不是微信，已停止真实点击以避免误操作。"
            )
    path = _win_debug_capture(account_name, stage)
    if not path:
        if _win_visual_strategy_enabled():
            raise WeChatForegroundError(
                "Windows visual 模式无法截取可验证的微信画面，已停止真实点击以避免误操作。"
            )
        return
    stats = _win_screenshot_stats(path)
    if _win_screenshot_looks_black(stats):
        raise WeChatForegroundError(
            "Windows visual 模式截到黑屏/无效画面，已停止真实点击以避免误操作。"
        )
    if _win_screenshot_looks_white_without_content(stats, _win_texts()):
        raise WeChatForegroundError(
            "Windows 微信主窗口疑似白屏，已停止前台点击以避免继续打乱窗口。"
            "请手动恢复/重启微信主窗口后重试。"
        )


def _win_screenshot_stats(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    script = r'''
Add-Type -AssemblyName System.Drawing
$bitmap = [System.Drawing.Bitmap]::FromFile($args[0])
$width = $bitmap.Width
$height = $bitmap.Height
$stepX = [Math]::Max(1, [int]($width / 96))
$stepY = [Math]::Max(1, [int]($height / 54))
$count = 0
$black = 0
$lumaSum = 0.0
for ($y = 0; $y -lt $height; $y += $stepY) {
  for ($x = 0; $x -lt $width; $x += $stepX) {
    $c = $bitmap.GetPixel($x, $y)
    $luma = (0.2126 * $c.R) + (0.7152 * $c.G) + (0.0722 * $c.B)
    $lumaSum += $luma
    $count += 1
    if ($luma -le 3) { $black += 1 }
  }
}
$bitmap.Dispose()
if ($count -le 0) { $count = 1 }
"{0}`t{1}`t{2}`t{3:N6}`t{4:N6}" -f $width, $height, $count, ($black / $count), ($lumaSum / $count)
'''
    try:
        out = _win_powershell(script, str(path), timeout=8.0).strip()
        width, height, sample_count, black_ratio, avg_luma = out.splitlines()[-1].split("\t")
        return {
            "width": int(width),
            "height": int(height),
            "sample_count": int(sample_count),
            "black_ratio": float(black_ratio),
            "avg_luma": float(avg_luma),
        }
    except Exception:
        return {}


def _win_print_visible_texts(account_name: str, stage: str, limit: int = 32) -> None:
    texts = sorted(_win_texts(), key=lambda item: (-item.y, item.x))
    preview = " | ".join(item.text for item in texts[:limit] if item.text)
    print(f"[wechat_foreground] Windows UIA 诊断 ({account_name}/{stage}): {preview}", flush=True)


def _win_find_top_left_search_placeholder() -> OCRText | None:
    candidates = [
        item for item in _win_texts()
        if "搜索" in item.text and item.x < 0.38 and item.y > 0.72
    ]
    return sorted(candidates, key=lambda item: (item.x, -item.y))[0] if candidates else None


def _win_find_account_result(account_name: str) -> OCRText | None:
    query = account_name.lower()
    texts = _win_texts()

    account_sections = [
        item for item in texts
        if item.text.strip() == "公众号"
        and item.x < 0.35
        and 0.12 < item.y < 0.92
    ]
    if account_sections:
        anchor = sorted(account_sections, key=lambda item: -item.y)[0]
        stop_sections = [
            item for item in texts
            if item.x < 0.35
            and item.y < anchor.y
            and item.text.strip() in {"聊天记录", "收藏", "更多", "聊天文件"}
        ]
        lower_bound = max((item.y for item in stop_sections), default=max(anchor.y - 0.22, 0.05))
        section_candidates = [
            item for item in texts
            if query in item.text.lower()
            and item.x < 0.55
            and lower_bound < item.y < anchor.y
            and "搜索" not in item.text
            and "网络查找" not in item.text
        ]
        if section_candidates:
            found = sorted(section_candidates, key=lambda item: -item.y)[0]
            return OCRText(
                found.text,
                max(0.045, min(found.x, 0.075)),
                found.y - 0.025,
                0.08,
                found.h + 0.05,
            )

        # OCR can miss the account-name text but still see the "公众号" section.
        # Click the first row under it and stay above chat-record/collection rows.
        return OCRText(account_name, 0.060, max(anchor.y - 0.145, lower_bound + 0.025), 0.10, 0.08)

    if _win_visual_strategy_enabled():
        # On the compact Windows search panel the followed public-account card
        # has a stable position below the "公众号" header. Avoid global text
        # matching here because chat records and favorites can contain the same
        # account name.
        return OCRText(account_name, 0.060, 0.715, 0.10, 0.08)

    candidates = [
        item for item in texts
        if query in item.text.lower()
        and item.x < 0.55
        and 0.12 < item.y < 0.82
        and "搜索" not in item.text
        and "网络查找" not in item.text
    ]
    return sorted(candidates, key=lambda item: -item.y)[0] if candidates else None


def _win_find_menu_or_button(labels: tuple[str, ...]) -> OCRText | None:
    matches = [
        item for item in _win_texts()
        if any(label in item.text for label in labels)
        and 0.35 < item.x < 0.99
    ]
    return sorted(matches, key=lambda item: (-item.y, item.x))[0] if matches else None


def _win_env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except ValueError:
        return default


def _win_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except ValueError:
        return default


def _win_point_sequence(name: str, default: str) -> list[tuple[float, float]]:
    value = os.getenv(name, default)
    points: list[tuple[float, float]] = []
    for raw in value.split(";"):
        parts = [part.strip() for part in raw.split(",", 1)]
        if len(parts) != 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return points


def _win_client_point_sequence(name: str, default: str) -> list[tuple[int, int]]:
    return [(int(x), int(y)) for x, y in _win_point_sequence(name, default)]


def _find_text_containing(text: str) -> OCRText | None:
    matches = [item for item in _ocr() if text in item.text]
    return sorted(matches, key=lambda item: -item.y)[0] if matches else None


def _find_menu_text_containing(text: str) -> OCRText | None:
    matches = [
        item for item in _ocr()
        if text in item.text
        and 0.45 < item.x < 0.99
        and 0.02 < item.y < 0.98
    ]
    return sorted(matches, key=lambda item: (-item.y, item.x))[0] if matches else None


def _find_text_exact(text: str) -> OCRText | None:
    matches = [item for item in _ocr() if item.text.strip() == text]
    return sorted(matches, key=lambda item: -item.y)[0] if matches else None


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Foreground WeChat URL collector utilities.")
    parser.add_argument(
        "--diagnose-windows",
        action="store_true",
        help="Print a no-click Windows WeChat foreground diagnostic JSON snapshot.",
    )
    parser.add_argument(
        "--preflight-windows-visual",
        action="store_true",
        help="Print a no-click readiness check for Windows visual foreground collection.",
    )
    parser.add_argument(
        "--no-capture",
        action="store_true",
        help="Skip diagnostic screenshot capture.",
    )
    parser.add_argument(
        "--scan-windows-cache",
        metavar="ACCOUNT",
        help="Print recent cached mp.weixin article URLs for a Windows WeChat account name.",
    )
    parser.add_argument(
        "--max-articles",
        type=int,
        default=10,
        help="Maximum articles for cache scanning utilities.",
    )
    parser.add_argument(
        "--target-date",
        default=None,
        help="Target publish date for cache scanning utilities, in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Inclusive lookback window ending at --target-date.",
    )
    args = parser.parse_args()

    if args.diagnose_windows:
        data = diagnose_windows_wechat_foreground(capture=not args.no_capture)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.preflight_windows_visual:
        data = preflight_windows_wechat_visual(capture=not args.no_capture)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if data.get("ready") else 2

    if args.scan_windows_cache:
        rows = _collect_wechat_article_urls_windows_cache(
            account_name=args.scan_windows_cache,
            max_articles=args.max_articles,
            target_date=args.target_date,
            days=args.days,
        )
        print(json.dumps([item.__dict__ for item in rows], ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
