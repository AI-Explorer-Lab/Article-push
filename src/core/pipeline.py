"""src/core/pipeline.py - Harness 流水线编排（方案A：编排层）。

职责（v3.0 编排层）：
1. 读取 harness.toml 配置 + AGENT.md 规则契约
2. 调用 agent.main() 执行全链路生成（agent 内部已完成抓取→阅读→写作→审稿→保存）
3. 验证产出：base report JSON + 逐篇 article 验证
4. 写错误日志
5. 不做生成、不做审稿——那些是 agent.py 的职责

与旧版的关键区别：
- 不再调用 writer.generate_articles()（agent.py 自己写文章）
- 不再运行审稿修订循环（agent.py 内置了）
- 只做编排 + 验证 + 错误日志
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
from src.infrastructure.error_logger import StageResult, write_error_log

load_dotenv()

# 兼容 tomllib（Python 3.11+）和 tomli（回退）
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore


ROOT = Path(__file__).resolve().parent.parent.parent
AGENT_RULES = ROOT / "AGENT.md"
HARNESS_CONFIG = ROOT / "harness.toml"
REPORTS_DIR = ROOT / "reports"
PAPER_DIR = ROOT / "daily_paper"
ERRORS_DIR = ROOT / "errors"
STATES_DIR = ROOT / "states"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_harness_config() -> dict:
    """加载 harness.toml 配置，返回 dict；文件不存在或解析失败时返回空 dict。"""
    if not HARNESS_CONFIG.exists():
        return {}
    try:
        if tomllib is None:
            print("[WARN] tomllib/tomli 不可用，无法读取 harness.toml，使用默认配置")
            return {}
        with HARNESS_CONFIG.open("rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        print(f"[WARN] harness.toml 解析失败: {exc}，使用默认配置")
        return {}


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


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------

def verify_selected_articles(report_path: Path, article_states: list[dict]) -> list[StageResult]:
    """对 article_states 中已产出的文章逐篇运行 verify_article。"""
    results: list[StageResult] = []
    seen: set[str] = set()
    for state in article_states:
        output_file = state.get("output_file")
        if not output_file or output_file in seen:
            continue
        if state.get("stage") not in (
            "kept_existing", "drafted", "revised", "review_passed", "review_failed",
        ):
            continue
        seen.add(output_file)
        output_path = ROOT / output_file
        if not output_path.exists():
            continue
        results.append(
            run_stage(
                f"verify article {Path(output_file).name}",
                [
                    sys.executable,
                    "-m",
                    "src.validators.verify_article",
                    output_file,
                    "--verbose",
                ],
            )
        )
    return results


def update_article_states(report_path: Path, results: list[StageResult], article_states: list[dict]) -> None:
    """将验证结果写回 article_states 文件。"""
    data = load_json(report_path)
    path = state_path(str(data["date"]))
    by_name = {Path(article.get("output_file", "")).name: article for article in article_states}
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
    write_json(path, {"date": data["date"], "articles": list(by_name.values())})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the full WeChat article harness pipeline (orchestration layer).")
    parser.add_argument("--date", default=date.today().isoformat(), help="Pipeline date in YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=10, help="Lookback window for agent.py.")
    parser.add_argument("--skip-fetch", action="store_true", help="Use existing report JSON instead of running agent.py.")
    parser.add_argument("--overwrite", action="store_true", help="Force re-run agent even if articles exist.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main() -> None:
    configure_console()
    args = parse_args()
    agent_rules = load_agent_rules()
    harness_cfg = load_harness_config()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORTS_DIR / f"{args.date}.json"
    results: list[StageResult] = []

    # 从 harness.toml 读取参数
    pipeline_cfg = harness_cfg.get("pipeline", {})
    target_count = pipeline_cfg.get("target_article_count", 5)
    max_github = pipeline_cfg.get("max_github", 2)

    print(f"Read rule contract: {project_path(AGENT_RULES)} ({len(agent_rules)} chars)")
    print(f"\n{'='*60}")
    print(f"Pipeline: {args.date} (编排层 v3.0)")
    print(f"目标: {target_count} 篇文章 (GitHub 最多 {max_github})")
    print(f"{'='*60}")

    # -----------------------------------------------------------------------
    # Stage 1: 运行 agent.main() 生成报告 + 文章
    # -----------------------------------------------------------------------
    if not args.skip_fetch:
        agent_cmd = [
            sys.executable,
            "-m",
            "src.core.agent",
            "--date",
            args.date,
            "--days",
            str(args.days),
        ]
        if args.overwrite:
            agent_cmd.append("--overwrite")
        results.append(run_stage("run agent (full pipeline)", agent_cmd))

        if not results[-1].ok:
            write_error_log(args.date, results, ERRORS_DIR)
            raise SystemExit(results[-1].returncode)
    elif not report_path.exists():
        raise SystemExit(f"Missing existing report: {project_path(report_path)}")

    # -----------------------------------------------------------------------
    # Stage 2: 验证 base report JSON
    # -----------------------------------------------------------------------
    results.append(
        run_stage(
            "verify base report",
            [sys.executable, "-m", "src.validators.verify", project_path(report_path)],
        )
    )
    if not results[-1].ok:
        write_error_log(args.date, results, ERRORS_DIR)
        raise SystemExit(results[-1].returncode)

    # -----------------------------------------------------------------------
    # Stage 3: 加载 article_states + 逐篇验证文章
    # -----------------------------------------------------------------------
    article_states_path = state_path(args.date)
    article_states: list[dict] = []

    if article_states_path.exists():
        try:
            states_data = load_json(article_states_path)
            article_states = states_data.get("articles", [])
            print(f"\n[INFO] 加载 {len(article_states)} 条 article states")
        except Exception as exc:
            print(f"[WARN] 读取 article_states 失败: {exc}")

    if article_states:
        article_results = verify_selected_articles(report_path, article_states)
        update_article_states(report_path, article_results, article_states)
        results.extend(article_results)

        if not all(r.ok for r in article_results):
            write_error_log(args.date, results, ERRORS_DIR)
            raise SystemExit(1)

    # -----------------------------------------------------------------------
    # 汇总
    # -----------------------------------------------------------------------
    passed = sum(1 for s in article_states if s.get("stage") == "verified")
    failed = sum(1 for s in article_states if s.get("stage") == "verify_failed")
    other = len(article_states) - passed - failed

    print("\n" + "=" * 60)
    print(f"Pipeline completed: {passed} 篇验证通过, {failed} 篇验证失败, {other} 篇其他状态")
    print("=" * 60)


if __name__ == "__main__":
    main()
