from types import SimpleNamespace
import os
import subprocess
import sys

import backend.deepseek_client as module
import backend.config as config


class FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(choices=[])


class FakeOpenAI:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def test_complete_passes_native_tool_call_request():
    completions = FakeCompletions()
    client = module.DeepSeekClient.__new__(module.DeepSeekClient)
    client._client = FakeOpenAI(completions)
    client.unconfigured = False
    client.reason = ""

    tools = [{"type": "function", "function": {"name": "recommend"}}]
    messages = [{"role": "user", "content": "15万以内帮我选车"}]
    client.complete(messages, tools)

    assert completions.kwargs == {
        "model": module.DEEPSEEK_MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "stream": False,
        "temperature": 0.1,
        "max_tokens": 65_536,
    }


def test_complete_omits_tool_choice_when_explicitly_disabled():
    completions = FakeCompletions()
    client = module.DeepSeekClient.__new__(module.DeepSeekClient)
    client._client = FakeOpenAI(completions)
    client.unconfigured = False
    client.reason = ""

    client.complete([], [], tool_choice=None)

    assert "tool_choice" not in completions.kwargs


def test_high_output_budget_has_matching_model_and_run_deadlines():
    assert config.CONTEXT_MAX_INPUT_TOKENS == 524_288
    assert config.CONTEXT_MAX_OUTPUT_TOKENS == 65_536
    assert config.CONTEXT_MAX_CONTEXT_TOKENS >= 593_920
    assert config.MODEL_MAX_OUTPUT_TOKENS >= 65_536
    assert config.MODEL_READ_TIMEOUT_S == 300
    assert config.SCHEDULER_RUN_TIMEOUT_S == 600
    assert config.RUN_BUDGET_LIMITS["active_execution_seconds"] == 600


def test_default_model_supports_configured_token_budgets():
    env = os.environ.copy()
    for key in ("DEEPSEEK_MODEL", "DEEPSEEK_API_KEY", "OWNER_COOKIE_SECRET"):
        env.pop(key, None)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    code = (
        "from backend.config import DEEPSEEK_MODEL, CONTEXT_MAX_CONTEXT_TOKENS, "
        "CONTEXT_MAX_INPUT_TOKENS, CONTEXT_MAX_OUTPUT_TOKENS; "
        "assert DEEPSEEK_MODEL == 'deepseek-v4-flash'; "
        "assert CONTEXT_MAX_CONTEXT_TOKENS >= "
        "CONTEXT_MAX_INPUT_TOKENS + CONTEXT_MAX_OUTPUT_TOKENS"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=config.ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_complete_clamps_provider_request_timeout_when_given():
    completions = FakeCompletions()
    client = module.DeepSeekClient.__new__(module.DeepSeekClient)
    client._client = FakeOpenAI(completions)
    client.unconfigured = False
    client.reason = ""
    client.complete([], [], timeout=1.25)
    assert completions.kwargs["timeout"] == 1.25


def test_thinking_model_detection_can_be_overridden(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_THINKING_MODE", "")
    assert config.is_thinking_model("deepseek-v4-flash") is True
    assert config.is_thinking_model("deepseek-reasoner") is True
    assert config.is_thinking_model("deepseek-chat") is False

    monkeypatch.setenv("DEEPSEEK_THINKING_MODE", "disabled")
    assert config.is_thinking_model("deepseek-v4-flash") is False
