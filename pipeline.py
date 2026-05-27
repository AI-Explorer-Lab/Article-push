"""End-to-end harness pipeline for the WeChat article workflow.

The pipeline makes the project flow explicit:
AGENT.md -> reports/YYYY-MM-DD.json -> reports/YYYY-MM-DD.deepread.json
-> daily_paper/*.md -> verify*.py.

It keeps hard-rule validation in the existing verify scripts. When a stage
fails, it writes a proposed lesson log to errors/YYYY-MM-DD-log.md for human
review; it never edits AGENT.md automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
AGENT_RULES = ROOT / "AGENT.md"
REPORTS_DIR = ROOT / "reports"
PAPER_DIR = ROOT / "daily_paper"
ERRORS_DIR = ROOT / "errors"


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class StageResult:
    name: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def project_path(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("/", "\\")


def run_stage(name: str, command: list[str]) -> StageResult:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    result = StageResult(
        name=name,
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    print_stage(result)
    return result


def print_stage(result: StageResult) -> None:
    status = "PASS" if result.ok else "FAIL"
    print(f"\n[{status}] {result.name}")
    print(" ".join(result.command))
    output = (result.stdout + "\n" + result.stderr).strip()
    if output:
        print(output)


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def slugify(value: str, max_len: int = 34) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "", value)
    value = re.sub(r"\s+", "", value)
    value = value.strip(". ")
    return (value[:max_len] or "AI技术观察")


def infer_article_type(item: dict) -> str:
    source = str(item.get("source", ""))
    title = str(item.get("title", ""))
    category = str(item.get("category", ""))
    url = str(item.get("url", ""))
    text = f"{source} {title} {category} {url}".lower()
    if "github.com" in text or "github" in text:
        return "工具型"
    if any(word in text for word in ["sdk", "api", "mcp", "context", "harness", "anthropic", "openai"]):
        return "解读型"
    return "主线型"


def article_title(item: dict, article_type: str) -> str:
    title = str(item.get("title", "")).strip(" .")
    url = str(item.get("url", ""))
    if article_type == "工具型" and "github.com/" in url:
        repo = url.rstrip("/").split("github.com/")[-1]
        name = repo.split("/")[-1] if "/" in repo else repo
        return f"这个 {name} 项目，真正考验的是落地边界"
    if article_type == "解读型":
        if "SDK" in title or "sdk" in title.lower():
            return "模型公司为什么盯上 SDK？"
        return "这条 AI 动态，真正值得看的是工程入口"
    compact = re.sub(r"GitHub 项目更新：", "", title)
    compact = compact.replace("！", "").replace("？", "")
    return f"{compact[:22]}，真正值得看的不是热闹"


def build_deepread(report_path: Path, deepread_path: Path, article_count: int) -> None:
    report = load_json(report_path)
    items = sorted(
        report.get("items", []),
        key=lambda row: (row.get("relevance", 0), row.get("date", "")),
        reverse=True,
    )[:article_count]
    selected = []
    for item in items:
        article_type = infer_article_type(item)
        title = article_title(item, article_type)
        output_file = f"daily_paper/{report['date']}-{slugify(title)}.md"
        selected.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "source": item.get("source", ""),
                "article_type": article_type,
                "output_file": output_file,
                "raw_text_status": "fetched" if item.get("evidence") else "partial",
                "selection_reason": selection_reason(item, article_type),
                "article_plan": {
                    "title": title,
                    "core_claim": core_claim(item, article_type),
                },
            }
        )

    deepread = {
        "date": report["date"],
        "source_report": project_path(report_path),
        "generation_rule": (
            "由 pipeline.py 从基础日报自动选择深挖条目。每条深挖新闻单独生成一篇公众号 "
            "Markdown，不把多条新闻合并为一篇总述。"
        ),
        "selection_criteria": {
            "relevance": "优先选择相关度高、贴合 AI Agent、MCP、Coding Agent、Harness Engineering 的条目。",
            "writeability": "优先选择有明确问题、工具边界、工程启发或趋势判断的条目。",
            "composition": "尽量覆盖主线新闻、趋势解读和工具案例。",
            "source_access": "优先使用已有 evidence 或可访问 URL 的条目。",
        },
        "selected_items": selected,
    }
    write_json(deepread_path, deepread)
    print(f"Generated {project_path(deepread_path)} with {len(selected)} selected items")


def selection_reason(item: dict, article_type: str) -> str:
    category = item.get("category", "AI")
    if article_type == "工具型":
        return f"该条目具备工具或项目属性，且与 {category} 相关，适合写成工程落地和边界分析。"
    if article_type == "解读型":
        return f"该条目体现开发者入口、上下文或基础设施变化，适合围绕 {category} 做趋势解读。"
    return f"该条目相关度较高，具备事件切入和传播冲突点，适合围绕 {category} 写成主线型文章。"


def core_claim(item: dict, article_type: str) -> str:
    category = item.get("category", "AI")
    if article_type == "工具型":
        return "Agent 工具真正的价值不只在能力展示，而在能否处理状态、权限、部署、失败和审查。"
    if article_type == "解读型":
        return "这类动态的重点不是单点功能，而是 AI 正在争夺进入真实系统的工程入口。"
    return f"这次事件的重点不只是新闻本身，而是它暴露了 {category} 进入工程化阶段的新门槛。"


def generate_articles(deepread_path: Path, overwrite: bool) -> None:
    data = load_json(deepread_path)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    for item in data.get("selected_items", []):
        output = ROOT / item["output_file"]
        if output.exists() and not overwrite:
            print(f"Keep existing {project_path(output)}")
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        text = render_article(item)
        output.write_text(text, encoding="utf-8")
        print(f"Generated {project_path(output)}")


def render_article(item: dict) -> str:
    article_type = item["article_type"]
    if article_type == "工具型":
        return render_tool_article(item)
    if article_type == "解读型":
        return render_analysis_article(item)
    return render_main_article(item)


def source_subject(item: dict) -> str:
    title = str(item.get("title", "这条 AI 动态"))
    title = re.sub(r"GitHub 项目更新：", "", title)
    return title.strip(" .")[:42] or "这条 AI 动态"


def render_main_article(item: dict) -> str:
    plan = item["article_plan"]
    subject = source_subject(item)
    claim = plan["core_claim"]
    return f"""# {plan['title']}

这次事件真正值得看的，不是标题里那一点热闹，而是它把 AI 进入真实研发流程的问题摆到了台前。围绕「{subject}」，我们看到的不是孤立新闻，而是一次关于能力、流程和验证边界的提醒：模型越会做事，系统越需要知道它为什么能做、做到哪一步、失败后怎么收场。

## 热点背后已经进入工程现场

这次事件的核心看点可以先压缩成一句话：{claim} 它不是简单说明模型又多会回答一个问题，而是在提示 AI 正在进入更长链路的任务过程。这个过程里有规划、工具、上下文、评测、人工确认，也有测试、权限和失败恢复。

为什么这件事重要？因为过去很多 AI 新闻强调的是结果，今天更值得看的却是过程。一个系统如果只能给出漂亮答案，它还停留在演示层；如果它能把目标拆开、在工具和工作流之间推进，并把证据留给人审查，它才开始接近生产系统。

## 真正的分水岭不是速度，而是闭环

很多人会先问：它是不是更快、更强、更像专家？这些当然重要，但不只是最重要的问题。真正的分水岭在于，这类能力能不能进入可验证闭环。比如任务从哪里开始，依赖哪些上下文，调用了什么工具，哪些步骤被测试覆盖，哪些判断需要人类确认。

这会改变开发者和内容生产者看 AI 的方式。过去我们常把模型当成一个回答器，输入问题，等待输出。进入工程现场后，它更像一个参与者：它需要遵守流程，需要暴露状态，需要接受评测，也需要在出错时留下可追溯线索。

## 影响会先落在工作流上

这类进展意味着，AI 产品的竞争正在从单次回答转向工作流能力。谁能把上下文、工具、权限、测试和日志组织好，谁就更容易让用户放心把任务交出去。否则，模型越能干，风险越难管。

举个例子，如果 AI 帮团队完成一次调研，真正有价值的不是它写了几段摘要，而是它能否说明信息来自哪里、哪些结论只是推断、哪些地方需要补证据、哪些任务已经完成。没有这些结构，结果再顺滑也很难进入严肃流程。

## 接下来应该看系统能力

接下来值得看的，不只是同类新闻还会不会出现，而是这些能力会不会被放进更稳定的 harness 里。也就是有没有明确输入、明确输出、明确验证、明确失败处理，以及能不能把经验沉淀为下一轮规则。

所以这条新闻的长期价值，不在于制造一次惊讶，而在于提醒我们：AI 的下一步不是单纯更会生成，而是更会被约束地执行。真正成熟的系统，应该既能把事情往前推，也能让人知道它为什么这样推、推到哪里、哪些地方还不能放心。
"""


def render_analysis_article(item: dict) -> str:
    plan = item["article_plan"]
    subject = source_subject(item)
    claim = plan["core_claim"]
    return f"""# {plan['title']}

为什么这条动态值得单独看？因为它不是普通功能更新，而是在提醒我们：AI 的竞争正在从模型能力，转向开发者入口、上下文组织和真实系统集成。围绕「{subject}」，真正的问题不是谁多发布了一个能力，而是谁能把能力变成稳定、可接入、可评测的工程链路。

## 趋势已经从模型走向入口

这类动态背后的核心判断是：{claim} 当模型能力逐渐接近，开发者会更关心 API、SDK、MCP、权限边界、日志和部署体验。谁能降低接入成本，谁就更容易出现在真实工作流里。

这不是一个纯市场问题，而是技术问题。模型要进入业务系统，必须理解上下文，调用工具，处理失败，并且让开发者知道每一步发生了什么。如果入口层做得粗糙，再强的模型也可能停留在 demo。

## 背后原因是工作流变长了

背后原因并不复杂：AI 任务正在变长。过去一次调用就能完成的场景，现在会变成多步执行、多人确认、多个系统协作。只靠聊天窗口承载这些流程，很快就会遇到状态丢失、权限不清、测试缺位和成本不可控的问题。

比如一个团队想把 AI 接入客服、研发或数据分析流程，开发者不会只问模型聪不聪明。他们会问：有没有稳定 SDK？错误怎么处理？日志能不能追踪？上下文如何隔离？权限能不能收紧？这些才是生产环境里的真实门槛。

## 技术含义是上下文要被工程化

技术上，这意味着 Context Engineering 和 Harness Engineering 会变得更重要。上下文不再只是提示词长度，而是任务状态、工具返回、权限范围、用户意图和历史决策的组合。harness 也不只是测试脚本，而是把输入、执行、评测和反馈串起来的外壳。

这会影响开发者的架构选择。API 和 SDK 要提供清晰边界，MCP 这类连接协议要解决工具接入问题，评测要覆盖真实任务而不是只看单轮回答。真正的难点，是把这些部件放到一个能长期运行的系统里。

## 影响会落到开发者习惯上

这类变化的影响，会先落在开发者身上。大家会从“我能不能调用模型”，转向“我能不能放心把模型放进流程”。前者看的是功能，后者看的是可观测性、可恢复性、权限控制和审查机制。

接下来应该看两件事。第一，这类入口能不能让开发者少写胶水代码。第二，它能不能在失败时给出足够清楚的证据链。因为真正的生产系统不怕出错，怕的是出错后没人知道错在哪里。

所以，这条动态的本质不是热闹，而是 AI 工程化继续往前走了一步。模型公司争夺的也不只是注意力，而是进入真实系统的默认路径。
"""


def render_tool_article(item: dict) -> str:
    plan = item["article_plan"]
    url = item.get("url", "")
    subject = source_subject(item)
    claim = plan["core_claim"]
    project_line = f"\n项目链接：{url}\n" if "github.com/" in url.lower() else ""
    return f"""# {plan['title']}

真正危险的 Agent，不是不会做事，而是太会做事却没人管。围绕「{subject}」，我更关心的不是它看起来多聪明，而是它有没有把目标、状态、工具、权限、部署和失败处理放进同一个工程框架里。{project_line}
## 它要解决的不是聊天问题

它是什么？可以理解成一个面向 Agent 落地的工具或项目外壳。它关注的不是让模型多说几句漂亮话，而是让 Agent 在真实任务里能被组织、被观察、被限制，也能在失败后恢复。

这件事的定位很重要。普通聊天产品以对话为中心，用户问一句，模型答一句。Agent 工具则以任务为中心：目标是什么，步骤怎么拆，工具怎么调，状态怎么保存，什么时候需要人类确认。

## 适合谁看，要看任务是不是会变长

适合谁？如果你在做个人助手、研发自动化、企业内部 Agent 平台，或者任何需要 AI 持续推进任务的产品，这类项目值得关注。因为任务一旦变长，单轮对话就不够了。

举个例子，让 AI 解释一段代码很简单；让 AI 在仓库里修问题、跑测试、记录变更、等待审查、根据失败日志继续修改，就完全是另一类系统。这里的关键不只是模型，而是工作流、权限、CI、日志和上下文。

## 技术看点在边界，而不是包装

技术看点可以放在几个关键词上：Agent、工具、上下文、工作流、权限、部署、测试和审查。{claim} 如果这些边界不清楚，Agent 越主动，系统风险越高。

我最关注的是它能不能把执行过程变成可追踪对象。比如每一步用了什么输入，调用了什么工具，产生了什么输出，哪些地方失败，哪些地方需要人工批准。没有这些记录，所谓自动化很容易变成不可复盘的黑箱。

## 真正要验证的是稳定性

需要验证的地方也很明确。第一，项目是不是只有概念，还是有可运行的任务模型。第二，失败后能不能恢复，而不是从头再来。第三，权限能不能收紧，避免 Agent 做超出边界的事。第四，部署是不是足够简单，能不能进入真实工作流。

所以不要只看项目介绍里的大词。真正决定它价值的，是它能不能承受重复任务、异常输入、工具失败和人工审查。Agent 工具如果没有这些约束，很容易只适合演示；有了这些约束，才可能成为团队愿意长期使用的基础设施。

## 接下来值得看工程闭环

接下来值得看的是，它会不会形成完整 harness：明确输入，明确执行步骤，明确验证规则，明确错误日志，再把踩过的坑沉淀成下一轮规则。只有这样，Agent 才不是一次性脚本，而是可以持续改进的系统。

这也是工具型项目最容易被低估的地方。真正有价值的不是界面多漂亮，而是能不能让人放心把任务交出去，并且在结果不对时知道应该从哪里查起。
"""


def proposed_lessons(results: list[StageResult]) -> list[str]:
    text = "\n".join(result.stdout + "\n" + result.stderr for result in results if not result.ok)
    lessons: list[str] = []
    if "UnicodeEncodeError" in text or "gbk" in text:
        lessons.append("运行 Python 验证脚本时设置 PYTHONIOENCODING=utf-8，避免 Windows 控制台编码导致误失败。")
    if "Markdown 必须以一级标题开头" in text:
        lessons.append("写入 Markdown 时必须使用无 BOM UTF-8，并确保文件首字符就是 '# '。")
    if "GitHub 项目类文章必须" in text:
        lessons.append("GitHub 项目类文章必须在开头 8 行内保留项目链接。")
    if "超短段落比例偏高" in text:
        lessons.append("公众号成稿应合并连续超短段落，优先使用 2-4 句话组成一个自然段。")
    if "编辑评分低于发布门槛" in text:
        lessons.append("成稿验证分数低于 75 时，不进入发布态，先按 verify_article.py 的 rewrite_suggestions 润色。")
    if not lessons:
        lessons.append("本次失败未匹配到已知坑，请人工阅读日志后决定是否补充新规则。")
    return lessons


def write_error_log(day: str, results: list[StageResult]) -> Path:
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    path = ERRORS_DIR / f"{day}-log.md"
    failed = [result for result in results if not result.ok]
    lines = [
        f"# {day} pipeline 运行问题记录",
        "",
        f"- 记录时间：{datetime.now().isoformat(timespec='seconds')}",
        "- 状态：需要人工确认后再写入 AGENT.md",
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
    print(f"Wrote failure log: {project_path(path)}")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full WeChat article harness pipeline.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Pipeline date in YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=10, help="Lookback window for agent.py.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum report items generated by agent.py.")
    parser.add_argument("--article-count", type=int, default=5, help="Number of Markdown articles to generate.")
    parser.add_argument("--skip-fetch", action="store_true", help="Use existing report JSON instead of running agent.py.")
    parser.add_argument("--refresh-deepread", action="store_true", help="Regenerate reports/YYYY-MM-DD.deepread.json.")
    parser.add_argument("--overwrite-articles", action="store_true", help="Regenerate Markdown articles even if they exist.")
    return parser.parse_args()


def main() -> None:
    configure_console()
    args = parse_args()
    if not AGENT_RULES.exists():
        raise SystemExit("AGENT.md not found; pipeline needs the project rule contract.")
    if not 3 <= args.article_count <= 5:
        raise SystemExit("--article-count must be between 3 and 5 to satisfy deepread rules.")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORTS_DIR / f"{args.date}.json"
    deepread_path = REPORTS_DIR / f"{args.date}.deepread.json"
    results: list[StageResult] = []

    print(f"Using rule contract: {project_path(AGENT_RULES)}")

    if not args.skip_fetch:
        results.append(
            run_stage(
                "generate base report",
                [
                    sys.executable,
                    "agent.py",
                    "--date",
                    args.date,
                    "--days",
                    str(args.days),
                    "--limit",
                    str(args.limit),
                ],
            )
        )
        if not results[-1].ok:
            write_error_log(args.date, results)
            raise SystemExit(results[-1].returncode)
    elif not report_path.exists():
        raise SystemExit(f"Missing existing report: {project_path(report_path)}")

    results.append(run_stage("verify base report", [sys.executable, "verify.py", project_path(report_path)]))
    if not results[-1].ok:
        write_error_log(args.date, results)
        raise SystemExit(results[-1].returncode)

    if args.refresh_deepread or not deepread_path.exists():
        build_deepread(report_path, deepread_path, args.article_count)
    else:
        print(f"Keep existing {project_path(deepread_path)}")

    generate_articles(deepread_path, overwrite=args.overwrite_articles)

    results.append(run_stage("verify deepread", [sys.executable, "verify_deepread.py", project_path(deepread_path)]))
    if not results[-1].ok:
        write_error_log(args.date, results)
        raise SystemExit(results[-1].returncode)

    results.append(
        run_stage(
            "verify articles",
            [
                sys.executable,
                "verify_article.py",
                "daily_paper",
                "--deepread",
                project_path(deepread_path),
                "--verbose",
            ],
        )
    )
    if not results[-1].ok:
        write_error_log(args.date, results)
        raise SystemExit(results[-1].returncode)

    print("\nPipeline completed: report, deepread, articles, and all validations passed.")


if __name__ == "__main__":
    main()
