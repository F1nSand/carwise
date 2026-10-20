"""可选、注入式 MCP 工具适配层。

这里不启动子进程、不访问网络，也不引入 MCP SDK；宿主可以注入实现了
list_tools/call_tool/close 的 provider。这样默认二手车 Agent 仍只有本地白名单工具。
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field, replace
from typing import Any, Protocol


class MCPProvider(Protocol):
    def list_tools(self) -> list[Any]: ...

    def call_tool(self, name: str, arguments: dict[str, Any], context: Any | None = None) -> Any: ...

    def close(self) -> Any: ...


@dataclass(frozen=True)
class MCPToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    source_id: str


@dataclass(frozen=True)
class MCPToolPolicy:
    idempotency: str = "read_only"
    timeout_s: float = 10.0
    max_attempts: int = 2
    retry_on: frozenset[str] = frozenset({"connect", "unavailable", "rate_limit"})
    max_concurrency: int = 4

    def __post_init__(self) -> None:
        if self.idempotency not in {"read_only", "idempotent_write", "non_idempotent"}:
            raise ValueError("MCP idempotency 非法")
        if self.timeout_s <= 0 or self.max_attempts <= 0 or self.max_concurrency <= 0:
            raise ValueError("MCP policy 数值必须为正数")


@dataclass(frozen=True)
class MCPInvocationContext:
    call_id: str
    idempotency_key: str
    deadline_monotonic: float
    cancellation: threading.Event
    run_id: str
    attempt: int


class MCPError(RuntimeError):
    code = "mcp_error"


class MCPConnectError(MCPError):
    code = "connect"


class MCPUnavailableError(MCPError):
    code = "unavailable"


class MCPRateLimitError(MCPError):
    code = "rate_limit"


class MCPApplicationError(MCPError):
    code = "application"


class MCPDeadlineExceeded(MCPError):
    code = "deadline_exceeded"


class MCPCancelled(MCPError):
    code = "cancelled"


class MCPUnknownOutcome(MCPError):
    code = "unknown_outcome"


@dataclass
class _Source:
    source_id: str
    provider: MCPProvider
    tools: dict[str, MCPToolDefinition]
    failures: int = 0
    opened_at: float | None = None
    half_open_used: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)
    executor: ThreadPoolExecutor | None = None
    quarantined: bool = False
    quarantined_tool: str | None = None
    tool_slots: dict[str, threading.BoundedSemaphore] = field(default_factory=dict)
    active_tools: dict[str, int] = field(default_factory=dict)


class MCPManager:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_s: float = 60.0,
        timeout_s: float = 10.0,
        retry_once: bool = True,
        policies: dict[str, MCPToolPolicy] | None = None,
        max_workers: int = 8,
    ) -> None:
        if failure_threshold < 1 or cooldown_s < 0 or timeout_s <= 0:
            raise ValueError("MCP 熔断和超时配置非法")
        self.failure_threshold = int(failure_threshold)
        self.cooldown_s = float(cooldown_s)
        self.timeout_s = float(timeout_s)
        self.retry_once = bool(retry_once)
        if max_workers <= 0:
            raise ValueError("MCP max_workers 必须为正数")
        self.max_workers = int(max_workers)
        self.policies = dict(policies or {})
        self._sources: dict[str, _Source] = {}
        self._model_index: dict[str, tuple[str, str]] = {}
        self._closed = False

    def register(
        self,
        source_id: str,
        provider: MCPProvider,
        allow_tools: set[str],
        policies: dict[str, MCPToolPolicy] | None = None,
    ) -> None:
        if self._closed:
            raise MCPUnavailableError("MCP manager 已关闭")
        source = str(source_id or "").strip()
        if not source or source in self._sources:
            raise ValueError("MCP source_id 为空或已注册")
        self.policies.update({f"{source}:{name}": policy for name, policy in (policies or {}).items()})
        definitions: dict[str, MCPToolDefinition] = {}
        for raw in provider.list_tools():
            definition = self._normalize_definition(raw, source)
            if definition.name in allow_tools:
                model_name = self.model_tool_name(source, definition.name)
                if model_name in self._model_index:
                    raise ValueError(f"MCP 工具名冲突：{model_name}")
                definitions[definition.name] = definition
                self._model_index[model_name] = (source, definition.name)
        source_state = _Source(source, provider, definitions)
        source_state.tool_slots = {
            name: threading.BoundedSemaphore(self._policy_for(source, name).max_concurrency)
            for name in definitions
        }
        source_state.executor = ThreadPoolExecutor(
            max_workers=min(self.max_workers, max((self._policy_for(source, name).max_concurrency for name in definitions), default=1)),
            thread_name_prefix=f"mcp-{source}",
        )
        self._sources[source] = source_state

    def snapshot_tools(self) -> list[dict[str, Any]]:
        schemas = []
        for source_id, source_name in sorted(self._model_index.values()):
            definition = self._sources[source_id].tools[source_name]
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": self.model_tool_name(source_id, source_name),
                        "description": definition.description,
                        "parameters": copy.deepcopy(definition.parameters),
                    },
                }
            )
        return schemas

    def has_model_tool(self, model_name: str) -> bool:
        return str(model_name) in self._model_index

    def idempotency_key(self, run_id: str, call_id: str) -> str:
        return hashlib.sha256(f"{run_id}:{call_id}".encode("utf-8")).hexdigest()

    def timeout_for(self, model_name: str) -> float:
        key = self._model_index.get(str(model_name))
        if key is None:
            return self.timeout_s
        return self._policy_for(*key).timeout_s

    def call_model_tool(
        self,
        model_name: str,
        arguments: dict[str, Any],
        *,
        context: MCPInvocationContext | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise MCPUnavailableError("MCP manager 已关闭")
        key = self._model_index.get(str(model_name))
        if key is None:
            raise KeyError(f"未注册的 MCP 工具：{model_name}")
        source_id, tool_name = key
        source = self._sources[source_id]
        if not self._allow(source):
            return {"ok": False, "tool": model_name, "error": f"MCP 源熔断中：{source_id}",
                    "error_code": "quarantined" if source.quarantined else "circuit_open"}
        policy = self._policy_for(source_id, tool_name)
        attempts = min(policy.max_attempts, 2 if self.retry_once else 1)
        if policy.idempotency == "non_idempotent":
            attempts = 1
        last_error = "未知 MCP 错误"
        last_code = "mcp_error"
        for attempt in range(attempts):
            base_context = context or MCPInvocationContext(
                call_id=f"mcp-{time.time_ns()}",
                idempotency_key=hashlib.sha256(f"{source_id}:{tool_name}".encode()).hexdigest(),
                deadline_monotonic=time.monotonic() + policy.timeout_s,
                cancellation=threading.Event(),
                run_id="",
                attempt=1,
            )
            call_context = replace(base_context, attempt=attempt + 1)
            if call_context.cancellation.is_set():
                raise MCPCancelled("MCP 调用已取消")
            try:
                raw = self._call_with_timeout(source, tool_name, arguments, call_context, policy)
                self._record_success(source)
                if isinstance(raw, dict) and "ok" in raw:
                    return copy.deepcopy(raw)
                return {"ok": True, "tool": model_name, "data": raw}
            except Exception as exc:  # noqa: BLE001 - provider boundary
                typed = self._classify_error(exc)
                if isinstance(typed, MCPUnknownOutcome):
                    last_error = str(typed)
                    last_code = typed.code
                    break
                exc = typed
                last_error = str(exc)[:300]
                last_code = getattr(exc, "code", "mcp_error")
                if attempt + 1 < attempts and getattr(exc, "code", "application") in policy.retry_on:
                    reconnect = getattr(source.provider, "reconnect", None)
                    if callable(reconnect):
                        try:
                            reconnect()
                        except Exception as reconnect_exc:  # noqa: BLE001
                            last_error = f"{last_error}; 重连失败：{str(reconnect_exc)[:200]}"
                    continue
        self._record_failure(source)
        return {
            "ok": False,
            "tool": model_name,
            "error": f"MCP 工具执行失败：{last_error}",
            "error_code": last_code,
        }

    def close(self) -> None:
        self._closed = True
        for source in self._sources.values():
            if source.executor is not None:
                source.executor.shutdown(wait=False, cancel_futures=True)
            try:
                source.provider.close()
            except Exception:
                pass

    def _policy_for(self, source_id: str, tool_name: str) -> MCPToolPolicy:
        return self.policies.get(f"{source_id}:{tool_name}", MCPToolPolicy(timeout_s=self.timeout_s, max_attempts=2))

    @staticmethod
    def _classify_error(exc: Exception) -> MCPError:
        if isinstance(exc, MCPError):
            return exc
        if isinstance(exc, (TimeoutError, FutureTimeoutError)):
            return MCPUnknownOutcome("MCP 调用超时，结果未知")
        text = str(exc).lower()
        if any(marker in text for marker in ("connect", "transport", "unavailable", "network", "down")):
            return MCPUnavailableError(str(exc))
        if "rate" in text or "429" in text:
            return MCPRateLimitError(str(exc))
        return MCPApplicationError(str(exc))

    @staticmethod
    def model_tool_name(source_id: str, tool_name: str) -> str:
        return f"mcp__{source_id}__{tool_name}"

    def _allow(self, source: _Source) -> bool:
        with source.lock:
            if source.quarantined:
                return False
            if source.failures < self.failure_threshold:
                return True
            if source.opened_at is None or time.monotonic() - source.opened_at < self.cooldown_s:
                return False
            if source.half_open_used:
                return False
            source.half_open_used = True
            return True

    @staticmethod
    def _record_success(source: _Source) -> None:
        with source.lock:
            source.failures = 0
            source.opened_at = None
            source.half_open_used = False

    def _record_failure(self, source: _Source) -> None:
        with source.lock:
            source.failures += 1
            if source.failures >= self.failure_threshold:
                source.opened_at = time.monotonic()
                source.half_open_used = False

    def _call_with_timeout(
        self,
        source: _Source,
        name: str,
        arguments: dict[str, Any],
        context: MCPInvocationContext,
        policy: MCPToolPolicy,
    ) -> Any:
        if source.quarantined or source.executor is None:
            raise MCPUnavailableError(f"MCP 源不可用：{source.source_id}")

        def invoke() -> Any:
            method = source.provider.call_tool
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                parameters = {}
            if len(parameters) >= 3 or "context" in parameters:
                return method(name, copy.deepcopy(arguments), context)
            return method(name, copy.deepcopy(arguments))

        slot = source.tool_slots[name]
        call_deadline = min(context.deadline_monotonic, time.monotonic() + policy.timeout_s)
        while True:
            if context.cancellation.is_set():
                raise MCPCancelled("MCP 调用已取消")
            remaining = call_deadline - time.monotonic()
            if remaining <= 0:
                raise MCPDeadlineExceeded("等待 MCP 并发名额时超过截止时间")
            if slot.acquire(timeout=min(0.1, remaining)):
                break
        with source.lock:
            source.active_tools[name] = source.active_tools.get(name, 0) + 1
        try:
            future = source.executor.submit(invoke)
        except Exception:
            with source.lock:
                source.active_tools[name] -= 1
            slot.release()
            raise

        def release_slot(_future: Any) -> None:
            with source.lock:
                source.active_tools[name] = max(0, source.active_tools.get(name, 1) - 1)
                if (policy.idempotency == "read_only" and source.quarantined_tool == name
                        and source.active_tools[name] == 0):
                    source.quarantined = False
                    source.quarantined_tool = None
                    source.failures = 0
                    source.opened_at = None
                    source.half_open_used = False
            slot.release()

        future.add_done_callback(release_slot)
        while True:
            if context.cancellation.is_set():
                future.cancel()  # succeeds only if the provider call has not started
                raise MCPCancelled("MCP 调用已取消；已运行的 provider 调用可能仍在后台结束。")
            remaining = call_deadline - time.monotonic()
            if remaining <= 0:
                with source.lock:
                    # A provider can finish in the same instant that its timeout
                    # expires. Do not quarantine a read whose result is already
                    # available; its completion callback may have run already.
                    if future.done():
                        return future.result()
                    source.quarantined = True
                    source.quarantined_tool = name
                raise MCPUnknownOutcome(f"MCP 工具超时（>{policy.timeout_s:g}s），结果未知")
            try:
                return future.result(timeout=min(0.1, remaining))
            except FutureTimeoutError:
                continue

    @staticmethod
    def _normalize_definition(raw: Any, source_id: str) -> MCPToolDefinition:
        if isinstance(raw, MCPToolDefinition):
            if raw.source_id != source_id:
                return MCPToolDefinition(raw.name, raw.description, raw.parameters, source_id)
            return raw
        if not isinstance(raw, dict):
            raise ValueError("MCP 工具定义格式非法")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("MCP 工具缺少 name")
        return MCPToolDefinition(
            name=name,
            description=str(raw.get("description") or ""),
            parameters=copy.deepcopy(raw.get("parameters") or {"type": "object"}),
            source_id=source_id,
        )
