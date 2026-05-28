"""src/core/pipeline.py - Harness 流水线编排。

架构（v2.3 审稿修订增强版）：
1. agent.py 直接抓取 5 篇候选（GitHub 最多 2，其他至少 3）→ reports/{date}.json
2. 验证基础日报
3. writer 直接从 report JSON 逐篇 AI 评估质量 + 生成 MD 初稿（好的写，不好的跳过）
4. 审稿 Agent 独立评审 → 不通过则带着审稿建议让 writer 修改 → 再审
   - 默认最多 3 轮（通过 harness.toml [llm].review_max_rounds 配置）
   - 逐轮调整修改策略强度（温和→强化→强力重构）
   - 退化检测：分数连续下降时提前终止，避免越改越差
5. 审稿全部通过后丢弃上下文记忆
6. 验证产出

核心创新：
- 写作 LLM 和审稿 Agent 使用不同的 LLM 实例（独立 API Key/Model）
- 审稿不通过时，文章上下文保留，writer 根据审稿建议重新修改
- 避免了「自己写自己打分」的 bias
- 去掉了 deepread 中间层，report JSON 直接驱动 writer
- 审稿修订有明确的轮数上限和退化终止策略
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

from src.common.utils import chinese_char_count, load_json, write_json
from src.infrastructure.error_logger import StageResult, write_error_log
from src.core.writer import generate_articles
from src.infrastructure.llm_client import create_llm_provider

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


def verify_selected_articles(report_path: Path, article_states: list[dict]) -> list[StageResult]:
    results: list[StageResult] = []
    seen: set[str] = set()
    for state in article_states:
        output_file = state.get("output_file")
        if not output_file or output_file in seen:
            continue
        if state.get("stage") not in ("kept_existing", "drafted", "revised", "review_passed", "review_failed"):
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
    harness_cfg = load_harness_config()

    # 从 harness.toml 读取审稿修订配置，命令行参数可覆盖
    llm_cfg = harness_cfg.get("llm", {})
    review_max_rounds_cfg = llm_cfg.get("review_max_rounds", 3)
    review_pass_score_cfg = llm_cfg.get("review_pass_score", 65)

    # 命令行 --llm-rewrite-attempts 覆盖配置文件
    MAX_REVISE_ROUNDS = (
        args.llm_rewrite_attempts if args.llm_rewrite_attempts > 0 else review_max_rounds_cfg
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORTS_DIR / f"{args.date}.json"
    results: list[StageResult] = []

    print(f"Read rule contract: {project_path(AGENT_RULES)} ({len(agent_rules)} chars)")
    print(f"\n{'='*60}")
    print(f"Pipeline: {args.date}")
    print(f"目标: {5} 篇文章 (GitHub 最多 2, 其他至少 3)")
    print(f"模式: {'LLM 写作' if args.use_llm_writer else '规则写作'}")
    print(f"审稿修订: 最多 {MAX_REVISE_ROUNDS} 轮, 通过门槛 {review_pass_score_cfg} 分")
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

    # Stage 3: 生成文章（含 AI 质量评估 + 边读边判边生成）
    # 直接从 report JSON 驱动，不再经过 deepread 中间层
    llm_provider = None
    if args.use_llm_writer:
        llm_provider = create_llm_provider(llm_model=args.llm_model)
        print(f"\n[LLM] LLM 模式启用: {llm_provider.model}")
        print("   每篇文章会先用 AI 评估原文质量，好的生成 MD，不好的跳过")

    contexts: dict[str, dict] = {}
    article_states: list[dict] = []
    try:
        article_states, contexts = generate_articles(
            report_path=report_path,
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

    # Stage 4: 审稿修订循环（对每篇文章：审稿 → 不通过则修改 → 再审）
    # 新增 v2.3 功能：
    #   - 从 harness.toml 读取 review_max_rounds
    #   - 逐轮策略调整（温和→强化→强力重构）
    #   - 退化检测（连续下降则提前终止）
    from src.validators.verify_article import _create_reviewer_provider, verify_article
    from src.core.writer import revise_article_with_review_feedback, discard_contexts, save_article_states

    reviewer = _create_reviewer_provider()
    if reviewer:
        print(f"\n[审稿 Agent] 已启用: {reviewer.model}")
        print(f"[审稿 Agent] 与写作 LLM 使用不同的 provider 实例，避免自己写自己打分的 bias")
    else:
        print("\n[审稿 Agent] 未配置（REVIEW_LLM_API_KEY），跳过审稿修订循环")

    # 从 harness.toml 读取审稿评分门槛（命令行不可覆盖，保持配置文件权威性）
    review_pass_threshold = harness_cfg.get("article", {}).get("min_score_for_publish", 65)

    if reviewer and llm_provider:
        print(f"\n{'='*60}")
        print(f"审稿修订循环（最多 {MAX_REVISE_ROUNDS} 轮, 通过门槛 {review_pass_threshold} 分）")
        print(f"{'='*60}")

        for state in article_states:
            if state.get("stage") not in ("drafted", "revised"):
                continue

            output_filename = state.get("output_file", "")
            output_path = ROOT / output_filename
            if not output_path.exists():
                continue

            context = contexts.get(Path(output_filename).name)
            score_history: list[dict] = []  # 追踪每轮评分变化: [{round, score, checks}]

            for round_num in range(1, MAX_REVISE_ROUNDS + 1):
                print(f"\n  [{output_filename}] 第 {round_num}/{MAX_REVISE_ROUNDS} 轮审稿...")
                result = verify_article(output_path, None, reviewer=reviewer)

                current_score = result["score"]
                score_history.append({
                    "round": round_num,
                    "score": current_score,
                    "checks": result.get("checks", {}),
                    "passed": result["passed"],
                })

                if result["passed"]:
                    print(f"  [{output_filename}] ✅ 审稿通过 (评分: {current_score})")
                    state["stage"] = "review_passed"
                    state["review_rounds"] = round_num
                    state["review_score"] = current_score
                    state["review_score_history"] = score_history
                    state["verification"] = {
                        "stage": "review_passed",
                        "passed": True,
                        "score": current_score,
                    }
                    break

                print(f"  [{output_filename}] ❌ 审稿未通过 (评分: {current_score}, 门槛: {review_pass_threshold})")
                suggestions = result.get("rewrite_suggestions", [])
                checks = result.get("checks", {})
                for s in suggestions:
                    print(f"    💬 {s}")

                # ---- 退化检测 ----
                if len(score_history) >= 2:
                    prev_score = score_history[-2]["score"]
                    if current_score <= prev_score:
                        print(f"  [{output_filename}] ⚠️ 评分从 {prev_score} 降至 {current_score}，可能越改越差")
                        # 连续两轮下降，提前终止
                        if len(score_history) >= 3:
                            two_ago = score_history[-3]["score"]
                            if current_score <= prev_score <= two_ago:
                                print(f"  [{output_filename}] 🛑 连续三轮评分下降（{two_ago}→{prev_score}→{current_score}），提前终止修改")
                                state["stage"] = "review_degraded"
                                state["review_rounds"] = round_num
                                state["review_score"] = current_score
                                state["review_score_history"] = score_history
                                state["verification"] = {
                                    "stage": "review_degraded",
                                    "passed": False,
                                    "score": current_score,
                                    "reason": f"连续三轮评分下降: {two_ago}→{prev_score}→{current_score}",
                                }
                                break

                # 已达最大轮数
                if round_num >= MAX_REVISE_ROUNDS:
                    print(f"  [{output_filename}] ⚠️ 已达最大修订轮数 ({MAX_REVISE_ROUNDS})，标记为审稿失败")
                    state["stage"] = "review_failed"
                    state["review_rounds"] = round_num
                    state["review_score"] = current_score
                    state["review_score_history"] = score_history
                    state["verification"] = {
                        "stage": "review_failed",
                        "passed": False,
                        "score": current_score,
                    }
                    break

                # 没有上下文，无法修改
                if not context or not context.get("original_text"):
                    print(f"  [{output_filename}] ⚠️ 无上下文可用，无法修改")
                    state["stage"] = "review_failed_no_context"
                    state["review_score_history"] = score_history
                    break

                # ---- 逐轮策略调整 ----
                # 第 1 轮: 温和修改（temperature 0.5，标准指令）
                # 第 2 轮: 强化修改（temperature 0.7，更具体的修改要求）
                # 第 3+ 轮: 强力重构（temperature 0.8，允许大改结构）
                strategy_intensity = _get_revision_strategy(round_num, current_score, score_history)

                print(f"  [{output_filename}] 🔄 根据审稿建议修改中... (策略: {strategy_intensity['label']})")
                original_text = str(context.get("original_text", ""))
                current_text = output_path.read_text(encoding="utf-8")
                try:
                    revised = revise_article_with_review_feedback(
                        article_text=current_text,
                        original_text=original_text,
                        item={
                            "article_type": state.get("article_type", ""),
                            "title": state.get("title", ""),
                        },
                        review_suggestions=suggestions,
                        review_checks=checks,
                        llm_provider=llm_provider,
                        strategy=strategy_intensity,
                    )
                    output_path.write_text(revised, encoding="utf-8")
                    state["stage"] = "revised"
                    state["revision_round"] = round_num
                    state["revision_strategy"] = strategy_intensity["label"]
                    print(f"  [{output_filename}] 📝 修改完成 ({chinese_char_count(revised)} 字)")
                except Exception as exc:
                    print(f"  [{output_filename}] ❌ 修改失败: {exc}")
                    state["stage"] = "revision_error"
                    state["revision_error"] = str(exc)
                    state["review_score_history"] = score_history
                    break

        # 审稿完成后丢弃所有上下文
        discard_contexts(contexts)

        # 写入最终状态文件
        github_count = sum(1 for s in article_states if "github.com/" in str(s.get("url", "")).lower() and s.get("stage") not in ("skipped_github_limit", "skipped_quality", "failed_fetch", "failed_generate"))
        generated_count = sum(1 for s in article_states if s.get("stage") in ("kept_existing", "drafted", "revised", "review_passed", "review_failed", "review_degraded"))
        save_article_states(report_path, ROOT, article_states, generated_count, github_count)

        # 统计审稿结果
        passed = sum(1 for s in article_states if s.get("stage") == "review_passed")
        failed = sum(1 for s in article_states if s.get("stage") in ("review_failed", "review_failed_no_context", "review_degraded"))
        degraded = sum(1 for s in article_states if s.get("stage") == "review_degraded")
        print(f"\n[审稿总结] 通过: {passed}, 未通过: {failed}")
        if degraded > 0:
            print(f"[审稿总结] 其中 {degraded} 篇因评分连续下降提前终止（退化检测）")

        if failed > 0:
            print("[审稿] 有文章未通过审稿，pipeline 将标记为失败")
            # 不直接 exit，让后续验证阶段报告详细错误
    else:
        # 没有审稿 Agent 或没有写作 LLM，直接丢弃上下文
        from src.core.writer import discard_contexts, save_article_states
        discard_contexts(contexts)
        github_count = sum(1 for s in article_states if "github.com/" in str(s.get("url", "")).lower() and s.get("stage") not in ("skipped_github_limit", "skipped_quality", "failed_fetch", "failed_generate"))
        generated_count = sum(1 for s in article_states if s.get("stage") in ("kept_existing", "drafted"))
        save_article_states(report_path, ROOT, article_states, generated_count, github_count)

    # Stage 5: 逐篇验证文章（最终门禁）
    article_results = verify_selected_articles(report_path, article_states)
    update_article_states(report_path, article_results, article_states)
    results.extend(article_results)
    if not all(result.ok for result in article_results):
        write_error_log(args.date, results, ERRORS_DIR)
        raise SystemExit(1)

    print("\n" + "=" * 60)
    print("Pipeline completed: report, articles, and all validations passed.")
    print("=" * 60)


# ---------------------------------------------------------------------------
# 审稿修订策略
# ---------------------------------------------------------------------------

def _get_revision_strategy(round_num: int, current_score: int, score_history: list[dict]) -> dict:
    """根据当前轮次和历史评分返回修改策略。

    逐轮升级策略：
    - 第 1 轮: gentle — 温和修改，temperature 0.5，针对性修补
    - 第 2 轮: moderate — 强化修改，temperature 0.7，要求更大幅度改写
    - 第 3+ 轮: aggressive — 强力重构，temperature 0.8，允许改写结构和开头
    - 如果上一轮评分下降 > 5 分，跳过直接进入下一档强度

    Returns:
        {"label": str, "temperature": float, "description": str}
    """
    # 检测上轮是否严重退化
    degraded = False
    if len(score_history) >= 2:
        prev = score_history[-2]["score"]
        if prev - current_score > 5:
            degraded = True

    strategies = {
        1: {
            "label": "gentle",
            "temperature": 0.5,
            "description": "温和修改：逐条对照审稿建议进行针对性修补，保持原有结构和大部分内容",
        },
        2: {
            "label": "moderate",
            "temperature": 0.7,
            "description": "强化修改：对扣分较多的维度做更大幅度的改写，可调整段落顺序和二级标题",
        },
    }

    # 第 3 轮及以后
    default_aggressive = {
        "label": "aggressive",
        "temperature": 0.8,
        "description": "强力重构：允许重写开头、重组文章结构、替换模板化表达，仅保留核心事实",
    }

    if degraded and round_num > 1:
        # 退化时直接跳到下一档强度
        strategy = strategies.get(round_num + 1, default_aggressive)
        strategy["label"] = strategy["label"] + "_degraded"
        strategy["description"] += "（上轮评分下降超过5分，升级修改强度）"
        return strategy

    return strategies.get(round_num, default_aggressive)


if __name__ == "__main__":
    main()
