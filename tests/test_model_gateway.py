from types import SimpleNamespace

import pytest

from backend.agent_runtime.model_gateway import inspect_response, complete_turn, failure_code


def _response(content="answer", *, finish_reason=None, tool_calls=None, refusal=None, response_id="req-1"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [], refusal=refusal)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(id=response_id, choices=[choice])


@pytest.mark.parametrize(
    "response,category",
    [
        (_response("answer", finish_reason="stop"), "text"),
        (_response("", finish_reason="tool_calls", tool_calls=[object()]), "tool_calls"),
        (_response("partial", finish_reason="length", tool_calls=[object()]), "truncated"),
        (_response("", finish_reason="content_filter"), "refusal"),
        (_response("", finish_reason="stop"), "empty"),
    ],
)
def test_model_response_categories_preserve_provider_metadata(response, category):
    turn = inspect_response(response, latency_ms=23)

    assert turn.category == category
    assert turn.finish_reason == response.choices[0].finish_reason
    assert turn.provider_request_id == "req-1"
    assert turn.latency_ms == 23


def test_gateway_times_the_provider_call():
    class Client:
        def complete(self, **_request):
            return _response("answer", finish_reason="stop")

    turn = complete_turn(Client(), messages=[], tools=[])

    assert turn.category == "text"
    assert turn.latency_ms >= 0


def test_model_failure_code_distinguishes_timeout_from_generic_provider_error():
    assert failure_code(TimeoutError("deadline")) == "model_timeout"
    assert failure_code(RuntimeError("provider rejected request")) == "model_error"
