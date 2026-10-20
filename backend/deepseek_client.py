# -*- coding: utf-8 -*-
"""DeepSeek OpenAI 兼容客户端，只提供原生 tool_calls 请求。"""
from __future__ import annotations

from typing import Any

from backend.config import (CONTEXT_MAX_OUTPUT_TOKENS, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
                            DEEPSEEK_MODEL, MODEL_CONNECT_TIMEOUT_S, MODEL_READ_TIMEOUT_S)


class DeepSeekClient:
    """DeepSeek 封装。缺 key / 缺依赖时不可用，但可安全构造。"""

    def __init__(self) -> None:
        self._client = None
        self.unconfigured = False
        self.reason = ""

        if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY.startswith("sk-xxx"):
            self.unconfigured = True
            self.reason = "未配置 DEEPSEEK_API_KEY（请在项目根 .env 中填写）"
            return

        try:
            from openai import OpenAI
            import httpx
        except Exception:
            self.unconfigured = True
            self.reason = "未安装 openai 包（请 pip install openai）"
            return

        self._client = OpenAI(
            api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL,
            timeout=httpx.Timeout(MODEL_READ_TIMEOUT_S, connect=MODEL_CONNECT_TIMEOUT_S),
            max_retries=0,
        )

    def is_available(self) -> bool:
        return (not self.unconfigured) and (self._client is not None)

    def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        model: str | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        stream: bool = False,
        temperature: float = 0.1,
        max_tokens: int = CONTEXT_MAX_OUTPUT_TOKENS,
        timeout: float | None = None,
    ):
        """调用同一个 Agent 模型；响应保留原始 assistant/tool_calls 对象。"""
        if not self.is_available():
            raise RuntimeError(f"DeepSeek 不可用：{self.reason}")
        request = {
            "model": model or DEEPSEEK_MODEL,
            "messages": messages,
            "tools": tools,
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        if timeout is not None:
            request["timeout"] = max(0.001, float(timeout))
        return self._client.chat.completions.create(**request)


_client_instance: DeepSeekClient | None = None


def get_client() -> DeepSeekClient:
    """全局单例。"""
    global _client_instance
    if _client_instance is None:
        _client_instance = DeepSeekClient()
    return _client_instance
