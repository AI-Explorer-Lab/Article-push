"""End-to-end harness pipeline for the WeChat article workflow.

The pipeline makes the project flow explicit:
AGENT.md -> reports/YYYY-MM-DD.json -> reports/YYYY-MM-DD.deepread.json
-> daily_paper/*.md -> verify*.py.

It keeps hard-rule validation in the existing verify scripts. When a stage
fails, it writes a proposed lesson log to errors/YYYY-MM-DD-log.md for human
review; it never edits AGENT.md automatically.

The run artifacts form an auditable episode package: source report, deepread
selection, generated articles, short-term state, failure attribution, and
verification output.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
AGENT_RULES = ROOT / "AGENT.md"
REPORTS_DIR = ROOT / "reports"
PAPER_DIR = ROOT / "daily_paper"
ERRORS_DIR = ROOT / "errors"
STATES_DIR = ROOT / "states"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

STYLE_CONTRACTS = {
    "主线型": {
        "position": "围绕一条具体事件推进，先讲清楚原文发生了什么，再解释为什么重要。",
        "sections": [
            "主体动作要先钉住",
            "变化发生在执行链路",
            "影响会落到具体角色",
            "后续只看可验证进展",
        ],
        "must_answer": ["发生了什么", "谁做了什么", "为什么值得单独写", "后续应该看什么"],
        "tone": "事实密度优先，判断跟在事实后面，不写空泛趋势口号。",
    },
    "解读型": {
        "position": "围绕一个趋势或技术信号展开，原文事实是证据，文章重点是解释变化背后的原因。",
        "sections": [
            "先限定这次信号的边界",
            "技术变化落在接入方式",
            "开发者会在流程里感到差异",
            "判断成本比判断热度更重要",
        ],
        "must_answer": ["趋势是什么", "原文事实如何支撑趋势", "技术含义是什么", "对行业或开发者有什么影响"],
        "tone": "解释要克制，避免把单条新闻拔高成确定趋势。",
    },
    "工具型": {
        "position": "围绕工具、项目或产品展开，先说明它是什么，再说明适合谁、边界在哪里。",
        "sections": [
            "先确认它解决的问题",
            "适用场景来自任务约束",
            "工程价值取决于生命周期",
            "边界要放到失败场景里看",
        ],
        "must_answer": ["它是什么", "适合谁", "技术看点是什么", "局限和验证点是什么"],
        "tone": "少写宣传词，多写使用场景、失败路径和工程约束。",
    },
}

GENERIC_TITLES = {
    "这条 AI 动态，真正值得看的是工程入口",
    "这个项目，真正考验的是落地边界",
    "这条动态值得拆开看",
    "真正值得看的不是热闹",
}


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


def state_path(day: str) -> Path:
    return STATES_DIR / f"{day}.article_states.json"


def fetch_text(url: str, timeout: int = 18) -> str:
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
    value = html.unescape(value or "")
    value = re.sub(r"<!--.*?-->", "", value, flags=re.S)
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def chinese_char_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


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
        return f"{name} 能不能进入工作流，要看状态和失败处理"
    compact = re.sub(r"GitHub 项目更新：", "", title)
    compact = compact.replace("！", "").replace("？", "")
    if article_type == "解读型":
        return f"{compact[:22]}，关键在接入方式怎么变"
    return f"{compact[:24]}，先看它改动了哪段流程"


def extract_required_terms(text: str, url: str = "") -> list[str]:
    terms: list[str] = []
    ignored = {
        "https",
        "http",
        "www",
        "com",
        "for",
        "true",
        "skip",
        "data-turbo-transient",
        "to",
    }
    for match in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", text):
        if match.lower() in ignored or "..." in match:
            continue
        terms.append(match)
    for match in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_.-]{3,}", text):
        if re.search(r"[\u4e00-\u9fff]", match) and len(match) <= 18:
            terms.append(match)
    if "github.com/" in url.lower():
        repo = url.rstrip("/").split("github.com/")[-1]
        terms.extend([part for part in repo.split("/") if part])

    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        normalized = term.strip("，。！？、：:；;（）()[]【】「」\"'")
        if len(normalized) < 3 or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= 8:
            break
    return result


def build_deepread(report_path: Path, deepread_path: Path, article_count: int) -> None:
    report = load_json(report_path)
    items = sorted(
        report.get("items", []),
        key=lambda row: (row.get("relevance", 0), row.get("date", "")),
        reverse=True,
    )[:article_count]
    selected = []
    used_outputs: set[str] = set()
    for item in items:
        article_type = infer_article_type(item)
        title = article_title(item, article_type)
        output_file = unique_output_file(report["date"], title, item, used_outputs)
        source_text = " ".join(
            str(item.get(key, "")) for key in ["title", "summary", "insight", "category"]
        )
        must_include = extract_required_terms(source_text, item.get("url", ""))
        selected.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "source": item.get("source", ""),
                "article_type": article_type,
                "output_file": output_file,
                "raw_text_status": "fetched" if item.get("evidence") else "partial",
                "selection_reason": selection_reason(item, article_type),
                "rewrite_policy": {
                    "based_on_original": True,
                    "memory_isolation": True,
                    "style_contract": STYLE_CONTRACTS[article_type]["position"],
                    "must_answer": STYLE_CONTRACTS[article_type]["must_answer"],
                },
                "article_plan": {
                    "title": title,
                    "core_claim": core_claim(item, article_type),
                    "must_include": must_include,
                    "original_summary": item.get("summary", ""),
                    "original_insight": item.get("insight", ""),
                },
            }
        )

    deepread = {
        "date": report["date"],
        "source_report": project_path(report_path),
        "generation_rule": (
            "由 pipeline.py 从基础日报自动选择深挖条目。该文件属于当次 harness episode 的"
            "上下文选择证据：每条深挖新闻必须逐篇读取原文，在原文事实基础上按文章类型改写；"
            "写完一篇即丢弃该篇上下文。"
        ),
        "harness_episode": {
            "task_spec": "AGENT.md",
            "context_selection": project_path(report_path),
            "state_file": project_path(state_path(str(report["date"]))),
            "verification": ["verify_deepread.py", "verify_article.py"],
            "completion_rule": "来源真实、选择有据、文章独立、验证通过、失败可归因。",
        },
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


def unique_output_file(day: str, title: str, item: dict, used_outputs: set[str]) -> str:
    base = slugify(title)
    output = f"daily_paper/{day}-{base}.md"
    if output not in used_outputs:
        used_outputs.add(output)
        return output

    hint = slugify(source_subject(item), max_len=18)
    output = f"daily_paper/{day}-{base}-{hint}.md"
    counter = 2
    while output in used_outputs:
        output = f"daily_paper/{day}-{base}-{hint}-{counter}.md"
        counter += 1
    used_outputs.add(output)
    return output


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
        return "Agent 工具的价值要落到状态、权限、部署、失败和审查这些生产问题上。"
    if article_type == "解读型":
        return "这类动态的重点不是单点功能，而是 AI 正在争夺进入真实系统的工程入口。"
    return f"这次事件暴露了 {category} 进入工程化阶段后必须处理的新门槛。"


def generate_articles(
    deepread_path: Path,
    overwrite: bool,
    use_llm_writer: bool = False,
    llm_model: str | None = None,
    llm_rewrite_attempts: int = 0,
    llm_max_original_chars: int = 120000,
) -> None:
    data = load_json(deepread_path)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    STATES_DIR.mkdir(parents=True, exist_ok=True)
    article_states: list[dict] = []
    for item in data.get("selected_items", []):
        output = ROOT / item["output_file"]
        base_state = {
            "date": data.get("date"),
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "article_type": item.get("article_type", ""),
            "output_file": item.get("output_file", ""),
            "context_scope": "single_article",
            "memory_policy": "discard_after_write",
            "stage": "pending",
        }
        if output.exists() and not overwrite:
            print(f"Keep existing {project_path(output)}")
            article_states.append(
                {
                    **base_state,
                    "stage": "kept_existing",
                    "output_exists": True,
                }
            )
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        text, rewrite_state = rewrite_one_article(
            item,
            use_llm_writer=use_llm_writer,
            llm_model=llm_model,
            llm_max_original_chars=llm_max_original_chars,
        )
        output.write_text(text, encoding="utf-8")
        if use_llm_writer and llm_rewrite_attempts > 0:
            text, rewrite_state = revise_article_with_llm_until_valid(
                item=item,
                output=output,
                deepread_path=deepread_path,
                current_text=text,
                rewrite_state=rewrite_state,
                llm_model=llm_model,
                max_attempts=llm_rewrite_attempts,
                llm_max_original_chars=llm_max_original_chars,
            )
        print(f"Generated {project_path(output)}")
        article_states.append(
            {
                **base_state,
                **rewrite_state,
                "stage": "drafted",
                "output_exists": output.exists(),
                "char_count": chinese_char_count(text),
            }
        )
        del text
    write_json(
        state_path(str(data["date"])),
        {
            "date": data["date"],
            "source_deepread": project_path(deepread_path),
            "harness_layers": {
                "context": "deepread 只保存当前文章需要的选择理由、写作计划和关键对象。",
                "tools": "抓取、生成和验证由固定脚本执行。",
                "orchestration": "pipeline.py 串联日报、deepread、文章和验证阶段。",
                "memory_state": "当天状态写入本文件；单篇上下文写完即丢弃。",
                "evaluation_observation": "验证结果回写到对应文章状态。",
                "constraints_recovery": "失败阶段写入 errors/，长期规则需人工确认。",
            },
            "state_policy": "短期状态只记录当天 pipeline episode；跨天长期记忆只进入 AGENT.md。",
            "articles": article_states,
        },
    )


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
                    "verify_article.py",
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


def source_subject(item: dict) -> str:
    title = str(item.get("title", "这条 AI 动态"))
    title = re.sub(r"GitHub 项目更新：", "", title)
    return title.strip(" .")[:42] or "这条 AI 动态"


def split_sentences(text: str) -> list[str]:
    pieces = re.split(r"(?<=[。！？.!?])\s*", text)
    sentences = []
    for piece in pieces:
        sentence = piece.strip()
        if 24 <= chinese_char_count(sentence) <= 180:
            sentences.append(sentence)
    return sentences


def title_terms(title: str) -> list[str]:
    return extract_required_terms(title)


def score_sentence(sentence: str, terms: list[str], article_type: str) -> int:
    score = sum(3 for term in terms if term and term in sentence)
    score += len(re.findall(r"\d+|AI|Agent|MCP|SDK|API|模型|工具|论文|项目|研究|代码|开发", sentence))
    if article_type == "工具型":
        score += len(re.findall(r"安装|使用|仓库|开源|项目|工具|部署|配置|运行", sentence))
    elif article_type == "解读型":
        score += len(re.findall(r"趋势|原因|意味着|背后|技术|行业|开发者", sentence))
    else:
        score += len(re.findall(r"发布|宣布|显示|开发|完成|提出|表示|事件|进展", sentence))
    return score


def fetch_original_context(item: dict) -> dict:
    url = str(item.get("url", ""))
    title = str(item.get("title", ""))
    article_type = item.get("article_type", "主线型")
    fallback = "。".join(
        part
        for part in [
            title,
            str(item.get("selection_reason", "")),
            str(item.get("article_plan", {}).get("core_claim", "")),
            str(item.get("article_plan", {}).get("original_summary", "")),
            str(item.get("article_plan", {}).get("original_insight", "")),
        ]
        if part
    )
    raw_text = ""
    fetch_status = "failed"
    if url:
        try:
            raw_text = clean_text(fetch_text(url))
            fetch_status = "fetched" if len(raw_text) >= 300 else "partial"
        except (HTTPError, URLError, TimeoutError, ValueError, OSError):
            raw_text = fallback
            fetch_status = "failed"
    text = raw_text if raw_text else fallback
    terms = title_terms(title)
    sentences = split_sentences(text)
    usable_sentences = [
        sanitize_fact_sentence(sentence)
        for sentence in sentences
        if is_usable_fact_sentence(sentence)
    ]
    ranked = sorted(
        usable_sentences,
        key=lambda sentence: score_sentence(sentence, terms, article_type),
        reverse=True,
    )
    facts = dedupe_sentences(ranked[:10])
    if len(facts) < 4:
        facts.extend(sentence for sentence in split_sentences(fallback) if sentence not in facts)
    if "github.com/" in url.lower() and len(facts) < 4:
        repo = url.rstrip("/").split("github.com/")[-1]
        facts.extend(
            [
                f"{repo} 是一个近期更新的 GitHub 项目，标题和仓库描述把它放在 AI Coding 语境下讨论。",
                "工具型项目要检查运行边界、失败处理、权限控制和日志线索。",
                "如果一个项目要进入团队工作流，部署、权限、日志和人工审查都需要提前设计。",
            ]
        )
    return {
        "status": fetch_status,
        "subject": source_subject(item),
        "terms": terms,
        "facts": facts[:8],
        "original_text": raw_text if fetch_status in {"fetched", "partial"} else "",
        "original_char_count": chinese_char_count(raw_text),
    }


def is_usable_fact_sentence(sentence: str) -> bool:
    noise_patterns = [
        "来源：",
        "量子位",
        "机器之心",
        "扫码",
        "相关阅读",
        "参考链接",
        "热门文章",
        "版权所有",
        "ICP备",
        "关于我们",
        "加入我们",
        "商务合作",
        "首页",
    ]
    if any(pattern in sentence for pattern in noise_patterns):
        return False
    if len(re.findall(r"\d{4}-\d{2}-\d{2}", sentence)) >= 2:
        return False
    return True


def sanitize_fact_sentence(sentence: str) -> str:
    sentence = re.sub(r"来源[:：].*$", "", sentence)
    sentence = re.sub(r"\s+", " ", sentence)
    return sentence.strip()


def dedupe_sentences(sentences: list[str]) -> list[str]:
    result: list[str] = []
    fingerprints: set[str] = set()
    for sentence in sentences:
        fingerprint = re.sub(r"\W+", "", sentence.lower())[:40]
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        result.append(sentence)
    return result


def soften_fact(sentence: str) -> str:
    sentence = re.sub(r"\s+", " ", sentence).strip()
    sentence = re.sub(r"^(近日|日前|今天|目前)[，,]?", "", sentence)
    sentence = sentence.strip(" 。")
    if not sentence:
        return ""
    return sentence + "。"


def fact_or_fallback(facts: list[str], index: int, fallback: str) -> str:
    if index < len(facts):
        return soften_fact(facts[index])
    return fallback


def require_llm_config(llm_model: str | None) -> tuple[str, str, str]:
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = llm_model or os.environ.get("LLM_MODEL")
    api_base = (os.environ.get("LLM_API_BASE") or "https://api.openai.com/v1").rstrip("/")
    if not api_key:
        raise RuntimeError("LLM writer needs LLM_API_KEY or OPENAI_API_KEY in the environment.")
    if not model:
        raise RuntimeError("LLM writer needs --llm-model or LLM_MODEL.")
    return api_base, api_key, model


def call_llm_chat(messages: list[dict], llm_model: str | None, temperature: float = 0.45) -> str:
    api_base, api_key, model = require_llm_config(llm_model)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    request = Request(
        f"{api_base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urlopen(request, timeout=240) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except (RemoteDisconnected, TimeoutError, URLError) as exc:
            last_error = exc
            if attempt == 3:
                raise
            time.sleep(attempt * 2)
    else:
        raise RuntimeError(f"LLM request failed: {last_error}")
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected LLM response shape: {data}") from exc
    return normalize_llm_article(str(content))


def normalize_llm_article(content: str) -> str:
    content = content.strip()
    fence_match = re.match(r"^```(?:markdown|md)?\s*(.*?)\s*```$", content, flags=re.S | re.I)
    if fence_match:
        content = fence_match.group(1).strip()
    return content.rstrip() + "\n"


def ensure_full_original_for_llm(context: dict, item: dict, max_chars: int) -> str:
    original_text = str(context.get("original_text") or "")
    status = context.get("status")
    if status != "fetched" or len(original_text) < 300:
        raise RuntimeError(
            "LLM writer requires fetched full original text for each article; "
            f"got status={status!r} for {item.get('title', '')!r}."
        )
    if len(original_text) > max_chars:
        raise RuntimeError(
            "Original text exceeds --llm-max-original-chars; refusing to excerpt automatically. "
            f"chars={len(original_text)}, limit={max_chars}, title={item.get('title', '')!r}."
        )
    return original_text


def build_llm_writer_prompt(item: dict, context: dict, original_text: str) -> list[dict]:
    plan = item.get("article_plan") or {}
    article_type = item.get("article_type", "")
    contract = STYLE_CONTRACTS.get(article_type, {})
    must_include = plan.get("must_include") or []
    github_note = ""
    if article_type == "工具型" and "github.com/" in str(item.get("url", "")).lower():
        github_note = f"- 这是 GitHub/开源项目文章，标题后 8 行内必须保留项目链接：{item.get('url')}\n"
    system = (
        "你是严谨的技术公众号编辑。你只能基于用户提供的原文全文写作，"
        "不得联网，不得添加原文没有的事实，不得把标题、关键词或摘要当作事实来源。"
    )
    user = f"""请基于下面的“原文全文”写一篇原创中文 Markdown 文章。

硬性规则：
- 只能使用原文全文中的事实；允许重组、解释、压缩和改写，但不允许新增事实。
- 如果原文信息不足，必须降低判断强度，不要补细节。
- 当前文章写完后，原文上下文会被丢弃；不要引用上一篇或其他文章的信息。
- Markdown 必须以一个一级标题开头，且只能有一个一级标题。
- 至少 3 个二级标题，二级标题必须写成具体判断。
- 中文正文 1000-1800 字。
- 不写“参考来源”“来源”“据某媒体报道”等归因区块。
- 不写“真正值得看”“背后的技术信号”“这条动态值得拆开看”“不只要看功能”等模板腔。
{github_note}
文章类型：{article_type}
类型写法：{contract.get("position", "")}
必须回答：{"；".join(contract.get("must_answer", []))}
标题建议：{plan.get("title", "")}
核心判断：{plan.get("core_claim", "")}
必须自然覆盖的关键对象：{"；".join(map(str, must_include))}

原文标题：{item.get("title", "")}
原文 URL：{item.get("url", "")}

原文全文：
{original_text}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def rewrite_one_article_with_llm(
    item: dict,
    context: dict,
    llm_model: str | None,
    llm_max_original_chars: int,
) -> tuple[str, dict]:
    original_text = ensure_full_original_for_llm(context, item, llm_max_original_chars)
    article = call_llm_chat(
        build_llm_writer_prompt(item, context, original_text),
        llm_model=llm_model,
    )
    rewrite_state = {
        "writer": "llm",
        "source_status": context.get("status"),
        "fact_count": len(context.get("facts", [])),
        "key_terms": context.get("terms", []),
        "original_char_count": context.get("original_char_count", 0),
        "context_discarded": True,
        "llm_model": llm_model or os.environ.get("LLM_MODEL"),
    }
    return article, rewrite_state


def rewrite_one_article(
    item: dict,
    use_llm_writer: bool = False,
    llm_model: str | None = None,
    llm_max_original_chars: int = 120000,
) -> tuple[str, dict]:
    article_type = item["article_type"]
    contract = STYLE_CONTRACTS[article_type]
    plan = item["article_plan"]
    context = fetch_original_context(item)
    if use_llm_writer:
        article, rewrite_state = rewrite_one_article_with_llm(
            item=item,
            context=context,
            llm_model=llm_model,
            llm_max_original_chars=llm_max_original_chars,
        )
        context.pop("original_text", None)
        del context
        return article, rewrite_state

    facts = context["facts"]
    title = str(plan["title"])
    subject = context["subject"]
    claim = str(plan.get("core_claim", ""))
    project_line = ""
    if article_type == "工具型" and "github.com/" in str(item.get("url", "")).lower():
        project_line = f"\n项目链接：{item['url']}\n"

    sections = contract["sections"]
    intro = build_intro(article_type, subject, claim, facts)
    body = [
        f"# {title}",
        "",
        intro + project_line,
        "",
        f"## {sections[0]}",
        "",
        build_first_section(article_type, facts, claim),
        "",
        f"## {sections[1]}",
        "",
        build_second_section(article_type, facts),
        "",
        f"## {sections[2]}",
        "",
        build_third_section(article_type, facts),
        "",
        f"## {sections[3]}",
        "",
        build_final_section(article_type, facts, contract),
        "",
    ]
    article = "\n".join(body)
    article = expand_if_short(article, article_type, subject)
    article = wrap_long_paragraphs(article)
    if chinese_char_count(article) < 1200:
        article = wrap_long_paragraphs(
            article.rstrip()
            + "\n\n"
            + f"补充一层编辑判断：围绕「{subject}」写作时，质量来自事实、判断和边界的配合。事实负责把对象说准，判断负责指出变化方向，边界负责避免把单条材料拔高成结论。"
            + "如果原文能提供足够细节，文章就多还原动作和流程；如果原文细节有限，文章就明确观察口径，用谨慎语气处理不确定部分。"
            + "当天短期状态也因此有必要保留：它能标出某篇文章是原文事实充足，还是主要依赖摘要和标题完成改写。两种情况都可以进入编辑流程，但发布判断应该不同。"
            + "\n"
        )
    rewrite_state = {
        "writer": "rule_based",
        "source_status": context.get("status"),
        "fact_count": len(facts),
        "key_terms": context.get("terms", []),
        "original_char_count": context.get("original_char_count", 0),
        "context_discarded": True,
    }
    context.pop("original_text", None)
    del context
    return article, rewrite_state


def verify_article_json(output: Path, deepread_path: Path) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            "verify_article.py",
            project_path(output),
            "--deepread",
            project_path(deepread_path),
            "--json",
        ],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse verify_article.py JSON output: {completed.stdout}") from exc
    if not payload:
        raise RuntimeError("verify_article.py returned an empty result list.")
    return payload[0]


def build_llm_revision_prompt(
    item: dict,
    context: dict,
    original_text: str,
    current_text: str,
    verification: dict,
) -> list[dict]:
    system = (
        "你是严谨的技术公众号编辑。你正在修订一篇未通过校验的文章。"
        "只能基于原文全文和当前文章修订，不得新增原文没有的事实。"
    )
    errors = "\n".join(f"- {msg}" for msg in verification.get("errors", [])) or "- 无"
    warnings = "\n".join(f"- {msg}" for msg in verification.get("warnings", [])) or "- 无"
    suggestions = "\n".join(f"- {msg}" for msg in verification.get("rewrite_suggestions", [])) or "- 无"
    user = f"""请修订下面的 Markdown 文章，让它通过校验。

硬性规则：
- 只能基于原文全文修订，不允许新增事实。
- 保留一个一级标题，至少 3 个二级标题。
- 不写“真正值得看”“背后的技术信号”“这条动态值得拆开看”“不只要看功能”等模板腔。
- 直接输出完整 Markdown，不要解释。

文章类型：{item.get("article_type", "")}
原文标题：{item.get("title", "")}
原文 URL：{item.get("url", "")}

校验错误：
{errors}

校验警告：
{warnings}

重写建议：
{suggestions}

当前文章：
{current_text}

原文全文：
{original_text}
"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def revise_article_with_llm_until_valid(
    item: dict,
    output: Path,
    deepread_path: Path,
    current_text: str,
    rewrite_state: dict,
    llm_model: str | None,
    max_attempts: int,
    llm_max_original_chars: int,
) -> tuple[str, dict]:
    attempts = 0
    for attempts in range(1, max_attempts + 1):
        verification = verify_article_json(output, deepread_path)
        if verification.get("passed"):
            rewrite_state["llm_revision_attempts"] = attempts - 1
            return current_text, rewrite_state

        context = fetch_original_context(item)
        original_text = ensure_full_original_for_llm(context, item, llm_max_original_chars)
        revised = call_llm_chat(
            build_llm_revision_prompt(item, context, original_text, current_text, verification),
            llm_model=llm_model,
            temperature=0.35,
        )
        output.write_text(revised, encoding="utf-8")
        current_text = revised
        rewrite_state["original_char_count"] = context.get("original_char_count", 0)
        context.pop("original_text", None)
        del context

    final_verification = verify_article_json(output, deepread_path)
    rewrite_state["llm_revision_attempts"] = attempts
    rewrite_state["llm_revision_passed"] = bool(final_verification.get("passed"))
    return current_text, rewrite_state


def expand_if_short(article: str, article_type: str, subject: str) -> str:
    if chinese_char_count(article) >= 1250:
        return article
    if article_type == "工具型":
        addition = f"""
把「{subject}」放到团队环境里看，判断标准会更清楚。一个要上线的工具，不能只在作者机器上跑通一次，还要能解释依赖什么环境、访问哪些资源、失败时留下什么线索。

Agent 工具一旦进入代码仓库、云资源或企业系统，权限、日志、回滚和审查都会变成硬问题。它是什么、适合谁、技术看点和局限，最后都要落到这些问题上。

换句话说，工具型文章的风格控制不靠固定模板，而靠问题清单：能不能运行，能不能接管，能不能审查，能不能在失败后继续。这些问题比一句漂亮介绍更接近真实使用。

如果后续继续观察这个项目，可以优先看三个变化：文档是否补齐真实任务示例，权限和状态是否能被清楚配置，失败日志是否能帮助下一次执行。只有这些细节变扎实，工具才有机会从候选仓库进入团队流程。

对读者来说，最实用的读法也很简单：先不要问它是不是很酷，而要问它能不能放进自己的工作流。如果答案还不清楚，就把它当成候选工具继续观察，而不是马上当成生产方案。
"""
    elif article_type == "解读型":
        addition = f"""
放到「{subject}」这个具体对象上，解读还需要多一层克制。它可以说明一个方向正在变热，但不能自动证明所有团队都会马上采用。分水岭在于开发者是否愿意把它接进已有流程。

所以后续应该看两个指标。第一，它有没有降低真实任务的接入成本；第二，它有没有让结果更容易检查和复盘。只有这两点成立，技术信号才会从一次热闹更新变成可持续的工作方式。

也可以换个角度看：如果这件事只带来一次新鲜感，它就是资讯；如果它改变了工具、上下文、权限或评测的组织方式，它才值得写成解读。

这也是解读型文章需要守住的边界。我们可以给出判断，但判断要能回到原文细节上；我们可以谈趋势，但趋势要能落到开发者下一步会怎么做、团队流程会怎么变。
"""
    else:
        addition = f"""
把「{subject}」放到具体团队流程里看，这次事件的意义会更清楚。它不是单纯告诉我们 AI 又能完成一个结果，而是在提醒团队重新划分任务：哪些步骤可以交给系统推进，哪些判断必须由人负责。

接下来应该看的不是一句口号，而是这套做法能不能反复运行。能反复运行，才说明它进入了工作流；只能偶尔惊艳一次，就仍然停留在案例层面。

主线型文章需要保持这个顺序：先让读者知道事实，再给判断；先还原动作，再谈影响。这样文章才像基于原文的改写，而不是换一个标题重新发挥。

所以这篇文章的收束点是可复用性：这次事件留下的是一次讨论热度，还是一种可以被更多人复用的方法。如果是后者，后续一定会出现更清楚的流程、边界和验证方式。
"""
    return article.rstrip() + "\n" + addition


def wrap_long_paragraphs(article: str) -> str:
    wrapped: list[str] = []
    for paragraph in article.split("\n\n"):
        stripped = paragraph.strip()
        if not stripped or stripped.startswith("#") or chinese_char_count(stripped) <= 170:
            wrapped.append(paragraph)
            continue
        sentences = re.split(r"(?<=[。！？])", stripped)
        current = ""
        chunks: list[str] = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if current and chinese_char_count(current + sentence) > 150:
                chunks.append(current.strip())
                current = sentence
            else:
                current += sentence
        if current:
            chunks.append(current.strip())
        wrapped.append("\n\n".join(chunks))
    return "\n\n".join(wrapped) + "\n"


def build_intro(article_type: str, subject: str, claim: str, facts: list[str]) -> str:
    first_fact = fact_or_fallback(facts, 0, f"原文围绕「{subject}」给出了一个具体对象和动作。")
    if article_type == "工具型":
        return (
            f"工具型项目先看定位，再看接入成本。围绕「{subject}」，需要问清楚它解决什么任务，"
            f"又把哪些工程边界暴露出来。{first_fact} 这篇文章会把它放回真实使用场景里看："
            "它能否安装、能否接进任务、能否留下状态，比一句能力描述更有信息量。"
        )
    if article_type == "解读型":
        return (
            f"这条动态可以作为一个技术信号来读，但边界要先说清楚。{first_fact} "
            f"我的判断是：{claim} 这个判断需要回到原文里的具体事实，不能只靠概念拔高。"
            "先看事实怎么发生，再看它可能改写哪一段开发者流程。"
        )
    return (
        f"这次事件先按事实读，再按流程读。{first_fact} 需要追问的是：它具体改变了哪段流程，"
        f"哪些环节仍然需要人来判断，以及它为什么会被放到今天的 AI Agent 语境里讨论。"
        "如果这几个问题讲不清楚，文章就只是在复述标题。"
    )


def build_first_section(article_type: str, facts: list[str], claim: str) -> str:
    first = fact_or_fallback(facts, 0, "原文最重要的信息，是它给出了一个具体事件，而不是抽象口号。")
    second = fact_or_fallback(facts, 1, "这类信息需要先还原动作、对象和结果，再谈影响。")
    if article_type == "工具型":
        return (
            f"先从原文给出的对象说起。{first} {second} 所以它不应该被简单理解成又一个“Agent 项目”，"
            "而要看它有没有把任务、工具、状态和使用者放进同一个可运行的流程里。适合谁，也要从这里判断："
            "如果团队只是想做一次演示，包装就够了；如果要让 AI 反复参与真实任务，就必须关心安装、配置、权限和失败恢复。"
        )
    if article_type == "解读型":
        return (
            f"原文给出的第一层信息是具体进展。{first} {second} 这些事实本身未必足够宏大，"
            f"但它们共同指向一个判断：{claim} 技术解读的第一步不是喊趋势，而是说明这些进展为什么会影响接入方式、"
            "任务分工或者成本结构。"
        )
    return (
        f"先看事实层。{first} {second} 如果只把它当成一条热闹新闻，就会漏掉流程问题："
        "这次事件到底是一次展示，还是已经开始改变某个真实工作流。为什么重要，也要从这里回答："
        "只有当流程被拆开、责任被重新分配，AI 的能力才不只是一次结果展示。"
    )


def build_second_section(article_type: str, facts: list[str]) -> str:
    third = fact_or_fallback(facts, 2, "原文里的关键细节，通常藏在流程描述和能力边界里。")
    fourth = fact_or_fallback(facts, 3, "这些细节决定它是一次演示，还是可以继续被复用的方法。")
    if article_type == "工具型":
        return (
            f"{third} {fourth} 这也是工具文章最需要保留的部分：不是替项目写宣传语，"
            "而是把它放进一个具体任务里，看看安装、调用、权限、失败处理和审查有没有位置。"
            "比如一个工具如果宣称能服务 Agent，就要继续追问：它能不能限制 Agent 能访问的资源，"
            "能不能记录每一步操作，能不能在出错后让人知道该从哪里恢复。"
        )
    if article_type == "解读型":
        return (
            f"{third} {fourth} 趋势判断不能脱离这些细节。有效的解读，是说明为什么这些小变化会改变入口、"
            "成本、开发者习惯或组织流程。比如开发者过去只需要关心一次调用，现在可能要关心上下文怎么传递、"
            "工具怎么接入、失败怎么复盘，以及哪些步骤需要人类确认。"
        )
    return (
        f"{third} {fourth} 这说明主线型文章不能只复述标题，必须把事件拆成几步：谁推进了什么，"
        "交给 AI 的是什么，仍然由人判断的又是什么。比如研究、写作、代码或调研任务里，AI 可以承担执行链路，"
        "但选题、判断标准和最终取舍仍然需要明确责任。"
    )


def build_third_section(article_type: str, facts: list[str]) -> str:
    fifth = fact_or_fallback(facts, 4, "从使用者视角看，影响工作的是能不能把结果接进下一步。")
    sixth = fact_or_fallback(facts, 5, "如果缺少验证和复盘，再漂亮的结果也很难变成稳定流程。")
    if article_type == "工具型":
        return (
            f"{fifth} {sixth} 举个例子，一个 Agent 工具如果只能完成理想输入下的单次任务，价值会很有限；"
            "如果它能记录步骤、暴露失败、允许人工接管，才更接近团队愿意长期使用的基础设施。"
            "技术看点也在这里：不是模型多会说，而是工具是否让任务生命周期可见，是否能把一次失败变成下一轮规则。"
        )
    if article_type == "解读型":
        return (
            f"{fifth} {sixth} 对开发者来说，变化往往不是突然发生在模型分数上，而是发生在接入方式、"
            "上下文组织、调试成本和团队协作习惯上。这里可以把它理解成一种工作流变化：AI 不再只负责最后的生成，"
            "而是开始进入准备、执行、检查和复盘这些中间环节。"
        )
    return (
        f"{fifth} {sixth} 这也是这类事件的现实影响：它让人重新分配人与 AI 的职责，"
        "把原本靠个人经验推进的环节，变成可以检查、可以复用、也可以追责的流程。影响不会只落在某个模型上，"
        "而会落在团队如何设计任务、如何留下证据、如何决定哪些步骤必须人工确认。"
    )


def build_final_section(article_type: str, facts: list[str], contract: dict) -> str:
    seventh = fact_or_fallback(facts, 6, "原文没有完全回答的问题，同样值得保留。")
    eighth = fact_or_fallback(facts, 7, "后续要看的不是一句口号，而是它能不能经受真实任务的反复检验。")
    if article_type == "工具型":
        return (
            f"{seventh} {eighth} 所以最后的判断要克制：少看它说自己能做什么，多看它在失败、权限、部署、"
            f"成本和人工审查面前怎么表现。{contract['tone']} 需要验证的不是一句介绍，而是它能否承受重复任务、"
            "异常输入和多人协作。"
        )
    if article_type == "解读型":
        return (
            f"{seventh} {eighth} 所以这篇文章的收束不是“趋势已定”，而是给出一个观察口径："
            f"以后看到类似动态，要看它是否真的降低了使用门槛，还是只换了一层包装。{contract['tone']} "
            "接下来更值得看的是真实用户会不会把它放进日常流程，而不是只在发布当天转发。"
        )
    return (
        f"{seventh} {eighth} 所以这件事的后续观察，不只是同类新闻还会不会出现，"
        "而是这套做法能不能在更多真实任务里保持稳定、透明和可复盘。"
    )


def proposed_lessons(results: list[StageResult]) -> list[str]:
    text = "\n".join(result.stdout + "\n" + result.stderr for result in results if not result.ok)
    lessons: list[str] = []
    if "UnicodeEncodeError" in text or "gbk" in text:
        lessons.append("运行 Python 验证脚本时设置 PYTHONIOENCODING=utf-8，避免 Windows 控制台编码导致误失败。")
    if "Markdown 必须以一级标题开头" in text:
        lessons.append("写入 Markdown 时必须使用无 BOM UTF-8，并确保文件首字符就是 '# '。")
    if "GitHub 项目类文章必须" in text:
        lessons.append("GitHub 项目类文章必须在开头 8 行内保留项目链接。")
    if "output_file 重复" in text:
        lessons.append("自动生成 deepread 时必须对 output_file 去重；同类泛化标题需要追加新闻关键词或序号。")
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

    try:
        generate_articles(
            deepread_path,
            overwrite=args.overwrite_articles,
            use_llm_writer=args.use_llm_writer,
            llm_model=args.llm_model,
            llm_rewrite_attempts=args.llm_rewrite_attempts,
            llm_max_original_chars=args.llm_max_original_chars,
        )
    except Exception as exc:
        results.append(
            StageResult(
                name="generate articles",
                command=[sys.executable, "pipeline.py", "--date", args.date],
                returncode=1,
                stdout="",
                stderr=f"{exc}\n\n{traceback.format_exc()}",
            )
        )
        print_stage(results[-1])
        write_error_log(args.date, results)
        raise SystemExit(1)

    results.append(run_stage("verify deepread", [sys.executable, "verify_deepread.py", project_path(deepread_path)]))
    if not results[-1].ok:
        write_error_log(args.date, results)
        raise SystemExit(results[-1].returncode)

    article_results = verify_selected_articles(deepread_path)
    update_article_states(deepread_path, article_results)
    results.extend(article_results)
    if not all(result.ok for result in article_results):
        write_error_log(args.date, results)
        raise SystemExit(1)

    print("\nPipeline completed: report, deepread, articles, and all validations passed.")


if __name__ == "__main__":
    main()
