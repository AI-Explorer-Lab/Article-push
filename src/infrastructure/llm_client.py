"""src/infrastructure/llm_client.py - LLM API 调用封装模块。

职责：
- 封装 OpenAI-compatible Chat Completions 接口调用
- 统一的错误处理、重试策略和响应解析
- 提供 LLM provider 抽象层，便于未来扩展
"""

from __future__ import annotations

import json
import os
import re
import time
from http.client import RemoteDisconnected
from typing import Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen


class LLMProvider(Protocol):
    """LLM 调用器接口协议。可替换实现以支持不同的 provider。"""

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.45,
        max_tokens: int = 4096,
    ) -> str: ...


class OpenAICompatibleProvider:
    """基于 OpenAI-compatible Chat Completions API 的 LLM 调用器。"""

    def __init__(
        self,
        api_base: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_retries: int = 3,
        request_timeout: int = 240,
    ):
        self.api_base = (api_base or os.environ.get("LLM_API_BASE") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
        self.model = model or os.environ.get("LLM_MODEL")
        self.max_retries = max_retries
        self.request_timeout = request_timeout

        if not self.api_key:
            raise RuntimeError(
                "LLM provider needs LLM_API_KEY or OPENAI_API_KEY in the environment."
            )
        if not self.model:
            raise RuntimeError(
                "LLM provider needs --llm-model or LLM_MODEL in the environment."
            )

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.45,
        max_tokens: int = 4096,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        request = Request(
            f"{self.api_base}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                with urlopen(request, timeout=self.request_timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except (RemoteDisconnected, TimeoutError, URLError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    raise RuntimeError(
                        f"LLM request failed after {self.max_retries} attempts: {exc}"
                    ) from exc
                time.sleep(attempt * 2)
        else:
            raise RuntimeError(f"LLM request failed: {last_error}")

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Unexpected LLM response shape: {data}") from exc
        return _normalize_llm_response(str(content))


def _normalize_llm_response(content: str) -> str:
    """清洗 LLM 返回的 Markdown 内容：去除代码围栏等。"""
    content = content.strip()
    fence_match = re.match(
        r"^```(?:markdown|md)?\s*(.*?)\s*```$",
        content,
        flags=re.S | re.I,
    )
    if fence_match:
        content = fence_match.group(1).strip()
    return content.rstrip() + "\n"


def create_llm_provider(
    llm_model: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
) -> OpenAICompatibleProvider:
    """创建 LLM provider 的便捷工厂函数。"""
    return OpenAICompatibleProvider(
        api_base=api_base,
        api_key=api_key,
        model=llm_model,
    )
