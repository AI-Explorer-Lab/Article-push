"""src/validators/verify_consistency.py - 自动化规则一致性校验脚本（精简版）。

检查以下一致性：
1. AGENT.md 中声明的关注领域 vs agent.py 中的 FOCUS_TOPICS
2. AGENT.md 中声明的关注领域 vs verify.py 中的 valid_categories
3. harness.toml 中的配置 vs 代码中的硬编码常量
4. 确保规则契约与代码实现之间没有漂移

v2.0 变更：不再检查 Anthropic News 等已移除的信息源。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # 需要 pip install tomli
    except ImportError:
        tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_MD = ROOT / "AGENT.md"
HARNESS_TOML = ROOT / "harness.toml"


def load_agent_md() -> str:
    return AGENT_MD.read_text(encoding="utf-8")


def extract_agreed_focus_areas(text: str) -> list[str]:
    """从 AGENT.md 的「关注领域」段落提取领域列表。

    格式支持：
    - AI Agent（涵盖 Agentic AI、Multi-Agent 等） -> 提取 "AI Agent"
    - MCP（Model Context Protocol） -> 提取 "MCP"
    每条一行，括号内的说明文字会被去除。
    """
    match = re.search(r"## 关注领域\n(.*?)(?:\n##|\Z)", text, re.S)
    if not match:
        return []
    section = match.group(1)
    raw_areas = re.findall(r"-\s+(.+?)(?:\n|$)", section)
    raw_areas = [a.strip() for a in raw_areas if a.strip()]

    # 提取领域名称：去除括号内的说明文字（支持中英文括号）
    areas = []
    for area in raw_areas:
        # 去除全角括号（）和半角括号 () 内的说明
        area_clean = re.sub(r"[（(][^)）]*[)）]", "", area).strip()
        if area_clean:
            areas.append(area_clean)

    return areas


def extract_python_dict_keys(file_path: Path, var_name: str) -> list[str]:
    """从 Python 文件中提取指定 dict 变量的键。

    优先使用 import 运行时方式（最可靠），回退到 AST 解析。
    """
    # 方法 1: 运行时导入（最可靠）
    try:
        module_name = file_path.stem
        if module_name == "agent":
            from src.core import agent as mod
            if hasattr(mod, var_name):
                return list(getattr(mod, var_name).keys())
    except (ImportError, AttributeError):
        pass

    # 方法 2: AST 解析（回退）
    try:
        source = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    if isinstance(node.value, ast.Dict):
                        keys = []
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant):
                                keys.append(str(key.value))
                        return keys
    return []


def extract_python_list(file_path: Path, var_name: str) -> list[str]:
    """从 Python 文件中提取指定 list 变量的值。

    优先使用 import 运行时方式（最可靠），回退到 AST 解析。
    """
    # 方法 1: 运行时导入（最可靠）
    try:
        module_name = file_path.stem
        if module_name == "verify":
            from src.validators import verify as mod
            if hasattr(mod, "verify_report"):
                # verify.py 没有模块级变量暴露 valid_categories
                # 回退到 AST 解析
                raise ImportError("use AST")
    except ImportError:
        pass

    # 方法 2: AST 解析
    try:
        source = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    if isinstance(node.value, ast.List):
                        items = []
                        for item in node.value.elts:
                            if isinstance(item, ast.Constant):
                                items.append(str(item.value))
                        return items
    return []


def load_toml() -> dict | None:
    if tomllib is None:
        return None
    try:
        with open(HARNESS_TOML, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return None


def check_consistency() -> tuple[bool, list[str], list[str]]:
    """执行一致性检查，返回 (通过, 错误, 警告)。"""
    errors: list[str] = []
    warnings: list[str] = []

    # 1. 检查 AGENT.md 是否存在
    if not AGENT_MD.exists():
        errors.append("AGENT.md 不存在，无法执行一致性检查")
        return False, errors, warnings

    agent_md = load_agent_md()
    agreed_areas = extract_agreed_focus_areas(agent_md)

    if not agreed_areas:
        errors.append("AGENT.md 中未找到「关注领域」列表")
        return False, errors, warnings

    # 2. 检查 agent.py 中的 FOCUS_TOPICS 键是否与 AGENT.md 一致
    focus_topics = extract_python_dict_keys(ROOT / "src" / "core" / "agent.py", "FOCUS_TOPICS")
    if focus_topics:
        md_only = set(agreed_areas) - set(focus_topics)
        py_only = set(focus_topics) - set(agreed_areas)
        for area in md_only:
            errors.append(
                f"AGENT.md 声明了关注领域「{area}」，但 agent.py 的 FOCUS_TOPICS 中未定义"
            )
        for area in py_only:
            errors.append(
                f"agent.py 的 FOCUS_TOPICS 中定义了「{area}」，但 AGENT.md 中未声明"
            )
    else:
        warnings.append("未能从 agent.py 中提取 FOCUS_TOPICS")

    # 3. 检查 verify.py 中的 valid_categories 是否覆盖 AGENT.md 的领域
    valid_categories = extract_python_list(ROOT / "src" / "validators" / "verify.py", "valid_categories")
    if valid_categories:
        for area in agreed_areas:
            if area not in valid_categories:
                warnings.append(
                    f"AGENT.md 关注领域「{area}」不在 verify.py 的 valid_categories 中，"
                    "分类验证可能失效"
                )
    else:
        warnings.append("未能从 verify.py 中提取 valid_categories")

    # 4. 检查 harness.toml 中的配置一致性
    toml = load_toml()
    if toml:
        toml_topics = toml.get("focus_topics", {}).get("topics", [])
        if toml_topics:
            for area in agreed_areas:
                if area not in toml_topics:
                    warnings.append(
                        f"AGENT.md 关注领域「{area}」不在 harness.toml focus_topics 中"
                    )
            for area in toml_topics:
                if area not in agreed_areas:
                    warnings.append(
                        f"harness.toml 关注领域「{area}」不在 AGENT.md 中"
                    )

        toml_cats = toml.get("valid_categories", {}).get("categories", [])
        if toml_cats and valid_categories:
            for cat in valid_categories:
                if cat not in toml_cats:
                    warnings.append(
                        f"verify.py valid_categories「{cat}」不在 harness.toml 中"
                    )
    else:
        warnings.append("harness.toml 无法解析（可能需要 pip install tomli），跳过 TOML 一致性检查")

    passed = len(errors) == 0
    return passed, errors, warnings


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    print("=" * 60)
    print("Harness 规则一致性检查")
    print("=" * 60)

    passed, errors, warnings = check_consistency()

    print("\n检查项: AGENT.md <-> agent.py <-> verify.py <-> harness.toml")
    print()

    for msg in errors:
        print(f"  [ERROR]   {msg}")
    for msg in warnings:
        print(f"  [WARN]    {msg}")

    print()
    print("=" * 60)
    if passed:
        if warnings:
            print(f"检查通过，共 {len(warnings)} 个警告（建议关注）。")
        else:
            print("所有一致性检查通过。")
        sys.exit(0)
    else:
        print(f"检查失败：{len(errors)} 个错误，{len(warnings)} 个警告。")
        print("请在修改 AGENT.md 或代码后，确保三者保持一致。")
        sys.exit(1)


if __name__ == "__main__":
    main()
