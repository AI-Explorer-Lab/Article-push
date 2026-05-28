"""src/common/verifier.py - 统一验证器接口模块。

为所有验证脚本定义一致的 Python 调用接口。
每个验证器提供 verify(path) -> VerifierResult 函数，
同时保留 if __name__ == "__main__" 的 CLI 入口。

pipeline.py 可以直接 import 并调用验证函数，
不再需要通过 subprocess 解析文本输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class VerifierResult:
    """统一的验证结果数据结构。"""
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # 验证器特定的额外数据

    def __bool__(self) -> bool:
        return self.passed
