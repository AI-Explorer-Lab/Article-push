"""src/core/pipeline.py - 精简版 Harness 流水线编排。

新架构（精简后）：
1. agent.py 直接抓取 5 篇候选（GitHub 最多 2，其他至少 3）
2. deepread 确认选题并生成计划
3. writer 逐篇 AI 评估质量 + 生成 MD（好的写，不好的跳过）
4. 验证产出
5. 每篇读完生成完，立即丢弃上下文

不再先选 10 篇再挑 5 篇。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import traceback
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from src.common.utils import load_json, write_json
from src.core.deepread import build_deepread
from src.infrastructure.error_logger import StageResult, write_error_log
from src.core.writer import generate_articles
from src.infrastructure.llm_client import create_llm_provider

load_dotenv()


ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_RULES = ROOT / "AGENT.md"
REPORTS_DIR = ROOT / "reports"
PAPER_DIR = ROOT / "daily_paper"
ERRORS_DIR = ROOT / "errors"
STATES_DIR = ROOT / "states"


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_agent_rules() -> str:
    if not AGENT_RULES.exists():
        raise SystemExit("AGENT.md not found; pipeline needs the project rule contract.")
    rules = AGENT_RULES.read_text(encoding="utf-8")
    if not rules.strip():
        raise SystemExit("AGENT.md is empty; pipeline needs a non-empty rule contract.")
    return rules


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


def state_path(day: str) -> Path:
    return STATES_DIR / f"{day}.article_states.json"


def verify_selected_articles(deepread_path: Path) -> list[StageResult]:
    data = load_json(deepread_path)
    results: list[StageResult] = []
    seen: set[str] = set()
    for item in data.get("selected_items", []):
        output_file = item.get("output_file")
        if not output_file or output_file in seen:
            continue
        seen.add(output_file)
        output_path = ROOT / output_file
        if not output_path.exists():
            continue  # 跳过的文章不验证
        results.append(
            run_stage(
                f"verify article {Path(output_file).name}",
                [
                    sys.executable,
                    "-m",
                    "src.validators.verify_article",
                    output_file,
                    "--deepread",
                    project_path(deepread_path),
                    "--verbose",
                ],
            )
        )
    return results


def update_article_states(deepread_path: Path, results: list[StageResult]) -> None:
    data = load_json(deepread_path)
    path = state_path(str(data["date"]))
    if not path.exists():
        return
    state_data = load_json(path)
    articles = state_data.get("articles", [])
    by_name = {Path(article.get("output_file", "")).name: article for article in articles}
    for result in results:
        match = re.search(r"verify article (.+\.md)$", result.name)
        if not match:
            continue
        file_name = match.group(1)
        article_state = by_name.get(file_name)
        if not article_state:
            continue
        score_match = re.search(r"评分:\s*(\d+)", result.stdout)
        article_state["verification"] = {
            "stage": "verified" if result.ok else "verify_failed",
            "passed": result.ok,
            "returncode": result.returncode,
            "score": int(score_match.group(1)) if score_match else None,
        }
        article_state["stage"] = article_state["verification"]["stage"]
    write_json(path, state_data)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full WeChat article harness pipeline (simplified).")
    parser.add_argument("--date", default=date.today().isoformat(), help="Pipeline date in YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=10, help="Lookback window for agent.py.")
    parser.add_argument("--skip-fetch", action="store_true", help="Use existing report JSON instead of running agent.py.")
    parser.add_argument("--refresh-deepread", action="store_true", help="Regenerate deepread.json.")
    parser.add_argument("--overwrite-articles", action="store_true", help="Regenerate Markdown articles even if they exist.")
    parser.add_argument("--use-llm-writer", action="store_true", help="Use LLM to evaluate quality and rewrite articles.")
    parser.add_argument("--llm-model", default=None, help="LLM model name.")
    parser.add_argument("--llm-rewrite-attempts", type=int, default=0, help="LLM revision attempts after verify fails.")
    parser.add_argument(
        "--llm-max-original-chars",
        type=int,
        default=120000,
        help="Fail instead of excerpting when original text exceeds this limit.",
    )
    return parser.parse_args()


def main() -> None:
    configure_console()
    args = parse_args()
    agent_rules = load_agent_rules()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORTS_DIR / f"{args.date}.json"
    deepread_path = REPORTS_DIR / f"{args.date}.deepread.json"
    results: list[StageResult] = []

    print(f"Read rule contract: {project_path(AGENT_RULES)} ({len(agent_rules)} chars)")
    print(f"\n{'='*60}")
    print(f"Pipeline: {args.date}")
    print(f"目标: {5} 篇文章 (GitHub 最多 2, 其他至少 3)")
    print(f"模式: {'LLM 写作' if args.use_llm_writer else '规则写作'}")
    print(f"{'='*60}")

    # Stage 1: 生成/复用基础日报
    if not args.skip_fetch:
        results.append(
            run_stage(
                "generate base report",
                [
                    sys.executable,
                    "-m",
                    "src.core.agent",
                    "--date",
                    args.date,
                    "--days",
                    str(args.days),
                ],
            )
        )
        if not results[-1].ok:
            write_error_log(args.date, results, ERRORS_DIR)
            raise SystemExit(results[-1].returncode)
    elif not report_path.exists():
        raise SystemExit(f"Missing existing report: {project_path(report_path)}")

    # Stage 2: 验证基础日报
    results.append(
        run_stage(
            "verify base report",
            [sys.executable, "-m", "src.validators.verify", project_path(report_path)],
        )
    )
    if not results[-1].ok:
        write_error_log(args.date, results, ERRORS_DIR)
        raise SystemExit(results[-1].returncode)

    # Stage 3: 生成/复用 deepread
    if args.refresh_deepread or not deepread_path.exists():
        build_deepread(report_path, deepread_path)
    else:
        print(f"Keep existing {project_path(deepread_path)}")

    # Stage 4: 生成文章（含 AI 质量评估 + 边读边判边生成）
    llm_provider = None
    if args.use_llm_writer:
        llm_provider = create_llm_provider(llm_model=args.llm_model)
        print(f"\n[LLM] LLM 模式启用: {llm_provider.model}")
        print("   每篇文章会先用 AI 评估原文质量，好的生成 MD，不好的跳过")

    try:
        generate_articles(
            deepread_path=deepread_path,
            root=ROOT,
            overwrite=args.overwrite_articles,
            use_llm_writer=args.use_llm_writer,
            llm_provider=llm_provider,
            llm_rewrite_attempts=args.llm_rewrite_attempts,
            llm_max_original_chars=args.llm_max_original_chars,
        )
    except Exception as exc:
        results.append(
            StageResult(
                name="generate articles",
                command=[sys.executable, "src/core/pipeline.py", "--date", args.date],
                returncode=1,
                stdout="",
                stderr=f"{exc}\n\n{traceback.format_exc()}",
            )
        )
        print_stage(results[-1])
        write_error_log(args.date, results, ERRORS_DIR)
        raise SystemExit(1)

    # Stage 5: 验证 deepread
    results.append(
        run_stage(
            "verify deepread",
            [sys.executable, "-m", "src.validators.verify_deepread", project_path(deepread_path)],
        )
    )
    if not results[-1].ok:
        write_error_log(args.date, results, ERRORS_DIR)
        raise SystemExit(results[-1].returncode)

    # Stage 6: 逐篇验证文章
    article_results = verify_selected_articles(deepread_path)
    update_article_states(deepread_path, article_results)
    results.extend(article_results)
    if not all(result.ok for result in article_results):
        write_error_log(args.date, results, ERRORS_DIR)
        raise SystemExit(1)

    print("\n" + "=" * 60)
    print("Pipeline completed: report, deepread, articles, and all validations passed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
