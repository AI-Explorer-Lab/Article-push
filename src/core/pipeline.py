"""src/core/pipeline.py - End-to-end harness pipeline for the WeChat article workflow.

The pipeline makes the project flow explicit:
AGENT.md -> reports/YYYY-MM-DD.json -> reports/YYYY-MM-DD.deepread.json
-> daily_paper/*.md -> verify*.py.

It keeps hard-rule validation in the existing verify scripts. When a stage
fails, it writes a proposed lesson log to errors/YYYY-MM-DD-log.md for human
review; it never edits AGENT.md automatically.

The run artifacts form an auditable episode package: source report, deepread
selection, generated articles, short-term state, failure attribution, and
verification output.

架构说明（重构后）：
src/common/utils.py:     公共工具（抓取、清洗、JSON读写）
src/common/verifier.py:  统一验证器数据类
src/core/context.py:     原文抓取与事实提取
src/core/deepread.py:    Deepread 选题生成
src/core/writer.py:      文章写作（规则 + LLM）
src/core/pipeline.py:    流程编排（仅此职责）
src/infrastructure/llm_client.py:   LLM API 调用封装
src/infrastructure/error_logger.py: 错误日志与建议规则
src/validators/verify*.py:          验证脚本
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from src.common.utils import chinese_char_count, load_json, write_json
from src.core.deepread import build_deepread
from src.infrastructure.error_logger import StageResult, write_error_log
from src.core.writer import generate_articles
from src.infrastructure.llm_client import create_llm_provider


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
        results.append(
            run_stage(
                f"verify article {Path(output_file).name}",
                [
                    sys.executable,
                    "src/validators/verify_article.py",
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
    parser = argparse.ArgumentParser(description="Run the full WeChat article harness pipeline.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Pipeline date in YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=10, help="Lookback window for agent.py.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum report items generated by agent.py.")
    parser.add_argument("--article-count", type=int, default=5, help="Number of Markdown articles to generate.")
    parser.add_argument("--skip-fetch", action="store_true", help="Use existing report JSON instead of running agent.py.")
    parser.add_argument("--refresh-deepread", action="store_true", help="Regenerate reports/YYYY-MM-DD.deepread.json.")
    parser.add_argument("--overwrite-articles", action="store_true", help="Regenerate Markdown articles even if they exist.")
    parser.add_argument("--use-llm-writer", action="store_true", help="Use an LLM to rewrite each article from the full original text.")
    parser.add_argument("--llm-model", default=None, help="LLM model name. Defaults to LLM_MODEL.")
    parser.add_argument("--llm-rewrite-attempts", type=int, default=1, help="LLM revision attempts after verify_article.py fails.")
    parser.add_argument(
        "--llm-max-original-chars",
        type=int,
        default=120000,
        help="Fail instead of excerpting when original text exceeds this character limit.",
    )
    return parser.parse_args()


def main() -> None:
    configure_console()
    args = parse_args()
    agent_rules = load_agent_rules()
    if not 3 <= args.article_count <= 5:
        raise SystemExit("--article-count must be between 3 and 5 to satisfy deepread rules.")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORTS_DIR / f"{args.date}.json"
    deepread_path = REPORTS_DIR / f"{args.date}.deepread.json"
    results: list[StageResult] = []

    print(f"Read rule contract: {project_path(AGENT_RULES)} ({len(agent_rules)} chars)")

    # Stage 1: 生成/复用基础日报
    if not args.skip_fetch:
        results.append(
            run_stage(
                "generate base report",
                [
                    sys.executable,
                    "src/core/agent.py",
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
            write_error_log(args.date, results, ERRORS_DIR)
            raise SystemExit(results[-1].returncode)
    elif not report_path.exists():
        raise SystemExit(f"Missing existing report: {project_path(report_path)}")

    # Stage 2: 验证基础日报
    results.append(
        run_stage(
            "verify base report",
            [sys.executable, "src/validators/verify.py", project_path(report_path)],
        )
    )
    if not results[-1].ok:
        write_error_log(args.date, results, ERRORS_DIR)
        raise SystemExit(results[-1].returncode)

    # Stage 3: 生成/复用 deepread
    if args.refresh_deepread or not deepread_path.exists():
        build_deepread(report_path, deepread_path, args.article_count)
    else:
        print(f"Keep existing {project_path(deepread_path)}")

    # Stage 4: 生成文章
    llm_provider = None
    if args.use_llm_writer:
        llm_provider = create_llm_provider(llm_model=args.llm_model)

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
            [sys.executable, "src/validators/verify_deepread.py", project_path(deepread_path)],
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

    print("\nPipeline completed: report, deepread, articles, and all validations passed.")


if __name__ == "__main__":
    main()
