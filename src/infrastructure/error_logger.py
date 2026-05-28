"""src/infrastructure/error_logger.py - 错误日志生成模块。

职责：
- 分析失败阶段并生成建议沉淀规则
- 写入 errors/YYYY-MM-DD-log.md
- 永不自动修改 AGENT.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class StageResult:
    """统一的阶段结果数据结构。"""
    name: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def proposed_lessons(results: list[StageResult]) -> list[str]:
    """分析失败输出，生成建议沉淀的规则。"""
    text = "\n".join(
        result.stdout + "\n" + result.stderr for result in results if not result.ok
    )
    lessons: list[str] = []
    if "UnicodeEncodeError" in text or "gbk" in text:
        lessons.append(
            "运行 Python 验证脚本时设置 PYTHONIOENCODING=utf-8，避免 Windows 控制台编码导致误失败。"
        )
    if "Markdown 必须以一级标题开头" in text:
        lessons.append(
            "写入 Markdown 时必须使用无 BOM UTF-8，并确保文件首字符就是 '# '。"
        )
    if "GitHub 项目类文章必须" in text:
        lessons.append(
            "GitHub 项目类文章必须在开头 8 行内保留项目链接。"
        )
    if "output_file 重复" in text:
        lessons.append(
            "自动生成 deepread 时必须对 output_file 去重；同类泛化标题需要追加新闻关键词或序号。"
        )
    if "超短段落比例偏高" in text:
        lessons.append(
            "公众号成稿应合并连续超短段落，优先使用 2-4 句话组成一个自然段。"
        )
    if "编辑评分低于发布门槛" in text:
        lessons.append(
            "成稿验证分数低于 75 时，不进入发布态，先按 verify_article.py 的 rewrite_suggestions 润色。"
        )
    if not lessons:
        lessons.append("本次失败未匹配到已知坑，请人工阅读日志后决定是否补充新规则。")
    return lessons


def write_error_log(
    day: str,
    results: list[StageResult],
    errors_dir: Path,
) -> Path:
    """写入错误日志到 errors/YYYY-MM-DD-log.md。"""
    errors_dir.mkdir(parents=True, exist_ok=True)
    path = errors_dir / f"{day}-log.md"
    failed = [result for result in results if not result.ok]
    lines = [
        f"# {day} pipeline 运行问题记录",
        "",
        f"- 记录时间：{datetime.now().isoformat(timespec='seconds')}",
        "- 状态：需要人工确认后再写入 AGENT.md",
        "- 用途：作为当次 harness episode 的失败归因材料，不自动改写长期规则",
        "",
        "## 失败阶段",
        "",
    ]
    for result in failed:
        lines.extend(
            [
                f"### {result.name}",
                "",
                f"- 命令：`{' '.join(result.command)}`",
                f"- 退出码：{result.returncode}",
                "",
                "```text",
                (result.stdout + "\n" + result.stderr).strip() or "(no output)",
                "```",
                "",
            ]
        )
    lines.extend(["## 建议沉淀规则", ""])
    for lesson in proposed_lessons(results):
        lines.append(f"- [ ] {lesson}")
    lines.extend(
        [
            "",
            "## 人工确认说明",
            "",
            "确认后再把勾选规则整理进 AGENT.md；pipeline.py 不会自动修改 AGENT.md。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote failure log: {path}")
    return path
