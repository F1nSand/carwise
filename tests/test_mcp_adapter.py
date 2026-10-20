import time

import pytest

from backend.agent_runtime.mcp import MCPInvocationContext, MCPManager, MCPToolDefinition, MCPToolPolicy, MCPUnknownOutcome


class FakeProvider:
    def __init__(self, definitions, results=None, failures=0):
        self.definitions = definitions
        self.results = results or {}
        self.failures = failures
        self.reconnects = 0
        self.calls = []

    def list_tools(self):
        return self.definitions

    def reconnect(self):
        self.reconnects += 1

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("transport down")
        return self.results.get(name, {"ok": True, "name": name, "arguments": arguments})

    def close(self):
        return None


def test_mcp_is_empty_until_explicitly_registered_and_filters_allowlist():
    manager = MCPManager()
    assert manager.snapshot_tools() == []
    provider = FakeProvider(
        [
            MCPToolDefinition("allowed", "允许工具", {"type": "object"}, "source-1"),
            MCPToolDefinition("blocked", "禁止工具", {"type": "object"}, "source-1"),
        ]
    )

    manager.register("source-1", provider, {"allowed"})

    schemas = manager.snapshot_tools()
    assert [item["function"]["name"] for item in schemas] == ["mcp__source-1__allowed"]


def test_mcp_reconnects_once_then_returns_result():
    provider = FakeProvider(
        [MCPToolDefinition("quote", "报价", {"type": "object"}, "source-2")],
        failures=1,
    )
    manager = MCPManager(retry_once=True)
    manager.register("source-2", provider, {"quote"})

    result = manager.call_model_tool("mcp__source-2__quote", {"series": "宝马3系"})

    assert result["ok"] is True
    assert provider.reconnects == 1
    assert len(provider.calls) == 2


def test_mcp_circuit_breaker_rejects_after_threshold_and_recovers_after_cooldown():
    provider = FakeProvider(
        [MCPToolDefinition("unstable", "不稳定", {"type": "object"}, "source-3")],
        failures=10,
    )
    manager = MCPManager(failure_threshold=2, cooldown_s=0.01, retry_once=False)
    manager.register("source-3", provider, {"unstable"})

    assert manager.call_model_tool("mcp__source-3__unstable", {})["ok"] is False
    assert manager.call_model_tool("mcp__source-3__unstable", {})["ok"] is False
    blocked = manager.call_model_tool("mcp__source-3__unstable", {})
    assert "熔断" in blocked["error"]
    time.sleep(0.02)
    probe = manager.call_model_tool("mcp__source-3__unstable", {})
    assert probe["ok"] is False


def test_mcp_rejects_unknown_model_tool():
    manager = MCPManager()
    with pytest.raises(KeyError):
        manager.call_model_tool("mcp__missing__tool", {})


def test_mcp_timeout_returns_without_waiting_for_provider_to_finish():
    class SlowProvider(FakeProvider):
        def call_tool(self, name, arguments):
            time.sleep(0.2)
            return super().call_tool(name, arguments)

    provider = SlowProvider([MCPToolDefinition("slow", "慢工具", {"type": "object"}, "source-4")])
    manager = MCPManager(timeout_s=0.01, retry_once=False)
    manager.register("source-4", provider, {"slow"})

    started = time.perf_counter()
    result = manager.call_model_tool("mcp__source-4__slow", {})
    elapsed = time.perf_counter() - started

    assert result["ok"] is False
    assert elapsed < 0.15


def test_mcp_non_idempotent_timeout_is_not_retried_and_returns_unknown_outcome():
    class SlowProvider(FakeProvider):
        def call_tool(self, name, arguments, context=None):
            time.sleep(0.05)
            return {"ok": True}

    provider = SlowProvider([MCPToolDefinition("write", "写入", {"type": "object"}, "source-write")])
    manager = MCPManager(
        timeout_s=0.005,
        retry_once=True,
        policies={"source-write:write": MCPToolPolicy(idempotency="non_idempotent", timeout_s=0.005, max_attempts=3)},
    )
    manager.register("source-write", provider, {"write"})
    result = manager.call_model_tool("mcp__source-write__write", {})
    assert result["ok"] is False
    assert "未知" in result["error"]
    assert len(provider.calls) == 0


def test_mcp_unknown_outcome_keeps_structured_code_and_read_only_recovers_after_task_finishes():
    class SlowProvider(FakeProvider):
        def call_tool(self, name, arguments, context=None):
            if not self.calls:
                time.sleep(0.04)
            self.calls.append((name, arguments))
            return {"ok": True}

    provider = SlowProvider([MCPToolDefinition("read", "只读", {"type": "object"}, "source-read")])
    manager = MCPManager(timeout_s=0.005, retry_once=False)
    manager.register("source-read", provider, {"read"},
                      {"read": MCPToolPolicy(idempotency="read_only", timeout_s=0.005)})
    first = manager.call_model_tool("mcp__source-read__read", {})
    assert first["error_code"] == "unknown_outcome"
    assert manager.call_model_tool("mcp__source-read__read", {})["error_code"] == "quarantined"
    time.sleep(0.06)
    assert manager.call_model_tool("mcp__source-read__read", {})["ok"] is True
    manager.close()


def test_mcp_policy_limits_concurrency_per_tool():
    import threading

    class CountingProvider(FakeProvider):
        def __init__(self):
            super().__init__([MCPToolDefinition("slow", "慢", {"type": "object"}, "source-cap")])
            self.active = 0
            self.peak = 0
            self.lock = threading.Lock()

        def call_tool(self, name, arguments, context=None):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.03)
            with self.lock:
                self.active -= 1
            return {"ok": True}

    provider = CountingProvider()
    manager = MCPManager(max_workers=4)
    manager.register("source-cap", provider, {"slow"},
                      {"slow": MCPToolPolicy(timeout_s=1, max_concurrency=1)})
    threads = [threading.Thread(target=manager.call_model_tool,
                                args=("mcp__source-cap__slow", {})) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert provider.peak == 1
    manager.close()


def test_mcp_policy_timeout_is_independent_of_outer_deadline():
    class SlowProvider(FakeProvider):
        def call_tool(self, name, arguments, context=None):
            time.sleep(0.05)
            return {"ok": True}

    provider = SlowProvider([MCPToolDefinition("read", "读取", {"type": "object"}, "source-deadline")])
    manager = MCPManager(retry_once=False)
    manager.register("source-deadline", provider, {"read"},
                     {"read": MCPToolPolicy(timeout_s=0.01, max_concurrency=1)})
    context = MCPInvocationContext("call", "key", time.monotonic() + 5, __import__("threading").Event(), "run", 1)
    started = time.monotonic()
    result = manager.call_model_tool("mcp__source-deadline__read", {}, context=context)
    elapsed = time.monotonic() - started
    assert result["error_code"] == "unknown_outcome"
    assert elapsed < 0.04
    manager.close()


def test_mcp_idempotency_key_is_stable_for_same_run_and_call():
    manager = MCPManager()
    assert manager.idempotency_key("run-1", "call-1") == manager.idempotency_key("run-1", "call-1")
    assert manager.idempotency_key("run-1", "call-1") != manager.idempotency_key("run-2", "call-1")
