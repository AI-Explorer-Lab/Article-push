"""src/common/utils.py - 公共工具模块。

提供抓取、文本清洗、JSON 读写等共享基础能力。
所有模块应通过导入本模块来获取这些能力，而非各自定义。
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)


def fetch_text(url: str, timeout: int = 18) -> str:
    """抓取指定 URL 的文本内容，自动检测编码。"""
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
    """清洗 HTML 文本：移除标签、脚本、样式、注释，合并空白。"""
    value = html.unescape(value or "")
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def strip_tags(value: str) -> str:
    """别名，等同于 clean_text。"""
    return clean_text(value)


def chinese_char_count(text: str) -> int:
    """统计文本中的中文字符数量。"""
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def slugify(value: str, max_len: int = 34) -> str:
    """将字符串转换为安全的文件名片段。"""
    value = re.sub(r"[\\/:*?\"<>|]", "", value)
    value = re.sub(r"\s+", "", value)
    value = value.strip(". ")
    return (value[:max_len] or "AI技术观察")


def load_json(path: Path) -> dict:
    """读取 JSON 文件。"""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    """写入 JSON 文件（自动创建父目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
