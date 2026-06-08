"""Foreground Mac WeChat URL collector.

This module drives the visible Mac WeChat client for the narrow part that
cannot be done reliably through Sogou/Chrome: finding same-day articles on a
public-account homepage and copying their mp.weixin.qq.com URLs.

It intentionally keeps the foreground-control surface small. The caller should
hand URLs to the normal background article fetcher after collection.
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


TMP_DIR = Path("/private/tmp")
DEBUG_DIR = TMP_DIR / "wechat_foreground_debug"


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


class WeChatForegroundError(RuntimeError):
    """Raised when foreground WeChat collection cannot continue safely."""


def collect_wechat_article_urls(
    account_name: str,
    max_articles: int = 3,
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
    if sys.platform != "darwin":
        raise WeChatForegroundError("foreground WeChat collection only supports macOS")

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
                seen_urls.add(url)
                urls.append(
                    WeChatArticleURL(
                        title=article.text,
                        url=url,
                        published_at=target.isoformat(),
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


def _is_interactive() -> bool:
    return sys.stdin.isatty() and os.getenv("WECHAT_FOREGROUND_ASSUME_READY") != "1"


def _run(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
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
