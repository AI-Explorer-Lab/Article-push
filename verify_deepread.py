"""verify_deepread.py - 深度阅读中间文件验证脚本"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path


VALID_ARTICLE_TYPES = {"主线型", "解读型", "工具型"}
VALID_RAW_STATUSES = {"fetched", "partial", "failed"}


def load_json(path: Path) -> tuple[dict | None, list[str]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f), []
    except json.JSONDecodeError as exc:
        return None, [f"JSON 格式错误: {exc}"]


def verify_deepread(file_path: str) -> tuple[bool, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    path = Path(file_path)
    if not path.exists():
        return False, [f"文件不存在: {file_path}"], []
    if path.suffix != ".json":
        errors.append(f"文件扩展名应为 .json: {path.name}")

    data, json_errors = load_json(path)
    if json_errors:
        return False, json_errors, warnings
    assert data is not None

    for key in ["date", "source_report", "selected_items"]:
        if key not in data or not data[key]:
            errors.append(f"缺少或为空的顶层字段: {key}")
    if errors:
        return False, errors, warnings

    date_value = str(data.get("date", ""))
    try:
        datetime.strptime(date_value, "%Y-%m-%d")
    except ValueError:
        errors.append(f"date 格式错误，应为 YYYY-MM-DD: {date_value}")

    source_report = Path(str(data["source_report"]))
    if not source_report.exists():
        warnings.append(f"source_report 指向的文件不存在: {source_report}")
    else:
        report_data, report_errors = load_json(source_report)
        if report_errors:
            warnings.append(f"source_report 无法解析: {'; '.join(report_errors)}")
        elif report_data and report_data.get("date") != date_value:
            warnings.append(
                f"source_report 日期与 deepread 日期不一致: {report_data.get('date')} != {date_value}"
            )

    items = data.get("selected_items")
    if not isinstance(items, list):
        errors.append("selected_items 必须是数组")
        return False, errors, warnings

    if not 3 <= len(items) <= 5:
        errors.append(f"selected_items 数量应为 3-5 条，当前为 {len(items)} 条")

    seen_urls: set[str] = set()
    seen_outputs: set[str] = set()
    required_fields = [
        "title",
        "url",
        "source",
        "article_type",
        "output_file",
        "raw_text_status",
        "selection_reason",
        "article_plan",
    ]

    for index, item in enumerate(items):
        tag = f"selected_items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{tag} 必须是对象")
            continue

        for field in required_fields:
            if field not in item or item[field] in ("", None, []):
                errors.append(f"{tag} 缺少或为空: {field}")

        article_type = item.get("article_type")
        if article_type and article_type not in VALID_ARTICLE_TYPES:
            errors.append(f"{tag} article_type 非法: {article_type}")

        raw_status = item.get("raw_text_status")
        if raw_status and raw_status not in VALID_RAW_STATUSES:
            errors.append(f"{tag} raw_text_status 非法: {raw_status}")
        elif raw_status == "failed":
            warnings.append(f"{tag} 原文抓取失败，不建议生成独立文章: {item.get('title', '?')}")

        url = str(item.get("url", ""))
        if url:
            if not re.match(r"^https?://", url):
                errors.append(f"{tag} url 必须是真实 http(s) 链接: {url}")
            if url in seen_urls:
                errors.append(f"{tag} url 重复: {url}")
            seen_urls.add(url)

        output_file = str(item.get("output_file", ""))
        if output_file:
            if output_file in seen_outputs:
                errors.append(f"{tag} output_file 重复: {output_file}")
            seen_outputs.add(output_file)

            output_path = Path(output_file)
            if not output_path.exists():
                errors.append(f"{tag} output_file 指向的 Markdown 不存在: {output_file}")
            elif output_path.suffix.lower() != ".md":
                errors.append(f"{tag} output_file 必须是 .md 文件: {output_file}")
            if not output_file.replace("\\", "/").startswith("daily_paper/"):
                warnings.append(f"{tag} output_file 建议位于 daily_paper/: {output_file}")
            if not Path(output_file).name.startswith(f"{date_value}-"):
                warnings.append(f"{tag} output_file 文件名建议以日期开头: {output_file}")

        article_plan = item.get("article_plan")
        if isinstance(article_plan, dict):
            for field in ["title", "core_claim"]:
                if not article_plan.get(field):
                    errors.append(f"{tag} article_plan 缺少或为空: {field}")
        elif article_plan is not None:
            errors.append(f"{tag} article_plan 必须是对象")

        reason = str(item.get("selection_reason", ""))
        if 0 < len(reason) < 20:
            warnings.append(f"{tag} selection_reason 过短，难以支撑编辑判断")

    return len(errors) == 0, errors, warnings


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python verify_deepread.py <reports/YYYY-MM-DD.deepread.json>")
        sys.exit(1)

    passed, errors, warnings = verify_deepread(sys.argv[1])

    print(f"\n{'=' * 50}")
    print(f"开始验证 deepread: {sys.argv[1]}")
    print(f"{'=' * 50}\n")

    for msg in errors:
        print(f"ERROR: {msg}")
    for msg in warnings:
        print(f"WARN: {msg}")

    print(f"\n{'=' * 50}")
    if passed:
        print(f"deepread 验证通过，共 {len(warnings)} 个警告。")
        sys.exit(0)
    print(f"deepread 验证失败：{len(errors)} 个错误，{len(warnings)} 个警告。")
    sys.exit(1)


if __name__ == "__main__":
    main()
