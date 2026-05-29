"""src/middleware/pipeline_logger.py - Pipeline 进度日志中间件。

职责：
- 统一日志格式（时间戳 + 阶段 + 消息）
- 同时输出到控制台 + 写入日志文件
- 支持阶段计时
- 异常自动捕获并记录堆栈

日志文件路径：logs/pipeline-YYYY-MM-DD.log
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = ROOT / "logs"


# ---------------------------------------------------------------------------
# 日志核心
# ---------------------------------------------------------------------------

class PipelineLogger:
    """Pipeline 进度日志器。"""

    def __init__(self, name: str = "pipeline", date_str: str = ""):
        self.name = name
        self.date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        self._start_times: dict[str, float] = {}
        self._log_path: Path | None = None
        self._file_handle: Any = None

    # ---- 文件输出 ----

    def _ensure_file(self) -> None:
        if self._log_path is not None:
            return
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._log_path = LOGS_DIR / f"{self.name}-{self.date_str}.log"
        # 追加模式，每次 pipeline 运行追加到同一天日志
        self._file_handle = self._log_path.open("a", encoding="utf-8")

    def _write(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{level}] {message}"
        # 控制台
        print(line, flush=True)
        # 文件
        try:
            self._ensure_file()
            if self._file_handle:
                self._file_handle.write(line + "\n")
                self._file_handle.flush()
        except Exception:
            pass  # 文件写入失败不影响控制台输出

    # ---- 级别方法 ----

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warn(self, message: str) -> None:
        self._write("WARN", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def debug(self, message: str) -> None:
        self._write("DEBUG", message)

    # ---- 阶段计时 ----

    def stage_start(self, stage_name: str) -> None:
        """标记一个阶段的开始。"""
        self._start_times[stage_name] = time.time()
        self.info(f"▶ 开始阶段: {stage_name}")

    def stage_end(self, stage_name: str, success: bool = True, extra: str = "") -> None:
        """标记一个阶段的结束，打印耗时。"""
        start = self._start_times.pop(stage_name, None)
        elapsed = f"{time.time() - start:.1f}s" if start else "?"
        icon = "✔" if success else "✖"
        msg = f"{icon} 完成阶段: {stage_name} (耗时 {elapsed})"
        if extra:
            msg += f" - {extra}"
        self.info(msg)

    # ---- 进度点 ----

    def step(self, current: int, total: int, label: str = "") -> None:
        """记录进度：第 N/M 步。"""
        pct = current / total * 100 if total > 0 else 0
        msg = f"[{current}/{total}] {pct:.0f}% {label}".strip()
        self.info(msg)

    # ---- 异常 ----

    def exception(self, message: str, exc: Exception) -> None:
        """记录异常（含堆栈）。"""
        self.error(f"{message}: {exc}")
        tb = traceback.format_exc()
        for line in tb.rstrip().split("\n"):
            self.error(f"  {line}")

    # ---- 分隔线 ----

    def separator(self, title: str = "") -> None:
        line = "=" * 60
        if title:
            self.info(line)
            self.info(f"  {title}")
            self.info(line)
        else:
            self.info(line)

    # ---- 资源清理 ----

    def close(self) -> None:
        if self._file_handle:
            self._file_handle.close()
            self._file_handle = None

    def __del__(self) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 模块级便捷函数（使用全局单例，适合 pipeline.py 调用）
# ---------------------------------------------------------------------------

_logger: PipelineLogger | None = None


def get_logger(date_str: str = "") -> PipelineLogger:
    """获取或创建全局日志器。"""
    global _logger
    if _logger is None:
        _logger = PipelineLogger(name="pipeline", date_str=date_str)
    return _logger


def init_logger(date_str: str) -> PipelineLogger:
    """初始化并返回日志器。"""
    global _logger
    _logger = PipelineLogger(name="pipeline", date_str=date_str)
    return _logger
