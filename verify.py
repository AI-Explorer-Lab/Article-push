"""verify.py - 技术日报的 Harness 验证脚本"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


def verify_report(file_path: str) -> tuple:
    """验证研究日报是否符合 AGENTS.md 中定义的规范"""
    errors = []
    warnings = []

    # ===== 1. 文件存在性 =====
    path = Path(file_path)
    if not path.exists():
        return False, [f"❌ 文件不存在: {file_path}"], []

    # ===== 2. JSON 格式 =====
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, [f"❌ JSON 格式错误: {e}"], []

    # ===== 3. 顶层结构 =====
    for key in ["date", "topic_focus", "items"]:
        if key not in data:
            errors.append(f"❌ 缺少顶层字段: {key}")
    if errors:
        return False, errors, warnings

    # ===== 4. 日期合法性 =====
    try:
        report_date = datetime.strptime(data["date"], "%Y-%m-%d")
        if report_date > datetime.now() + timedelta(days=1):
            errors.append(f"❌ 日期不能是未来日期: {data['date']}")
    except ValueError:
        errors.append(f"❌ 日期格式错误: {data['date']}")

    # ===== 5. 条目逐一检查 =====
    items = data.get("items", [])
    if len(items) == 0:
        errors.append("❌ 条目列表为空，至少需要 1 条技术动态")

    required_fields = [
        "title", "source", "url", "date",
        "category", "summary", "insight", "relevance"
    ]
    valid_categories = [
        "AI Agent", "Harness Engineering", "Context Engineering",
        "MCP", "LLM推理与优化", "多模态大模型",
        "AI Coding", "Vibe Coding", "其他"
    ]

    seen_urls = set()
    for i, item in enumerate(items):
        tag = f"条目[{i}]"

        # 必填字段
        for field in required_fields:
            if field not in item or not item[field]:
                errors.append(f"❌ {tag} 缺少或为空: {field}")

        # 分类合法性
        cat = item.get("category", "")
        if cat and cat not in valid_categories:
            warnings.append(f"⚠️ {tag} 分类不在预定义列表: {cat}")

        # 相关度评分
        rel = item.get("relevance", 0)
        if not isinstance(rel, (int, float)) or rel < 1 or rel > 5:
            errors.append(f"❌ {tag} 相关度必须在 1-5 之间: {rel}")
        elif rel < 3:
            errors.append(
                f"❌ {tag} 相关度 < 3，不应收录: {item.get('title', '?')}"
            )

        # 摘要长度
        summary = item.get("summary", "")
        if len(summary) > 100:
            warnings.append(f"⚠️ {tag} 摘要超100字({len(summary)}字)")

        insight = item.get("insight", "")
        if len(insight) > 150:
            warnings.append(f"⚠️ {tag} 洞察超150字({len(insight)}字)")

        # URL 去重
        url = item.get("url", "")
        if url in seen_urls:
            errors.append(f"❌ {tag} URL 重复: {url}")
        seen_urls.add(url)

    has_errors = len(errors) > 0
    return not has_errors, errors, warnings


def main():
    if len(sys.argv) < 2:
        print("用法: python verify.py <report.json>")
        sys.exit(1)

    file_path = sys.argv[1]
    print(f"\n{'='*50}")
    print(f"🔍 开始验证: {file_path}")
    print(f"{'='*50}\n")

    passed, errors, warnings = verify_report(file_path)

    for msg in errors:
        print(msg)
    for msg in warnings:
        print(msg)

    print(f"\n{'='*50}")
    if passed:
        print(f"🎉 验证通过！共 {len(warnings)} 个警告。")
        print("日报格式正确，可以发布。")
        sys.exit(0)
    else:
        print(f"💥 {len(errors)} 个错误, {len(warnings)} 个警告")
        print("请根据上述错误信息修复后重新验证。")
        sys.exit(1)


if __name__ == "__main__":
    main()