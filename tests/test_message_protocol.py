import pytest

from backend.agent_runtime.message_protocol import MessageProtocolError, validate_message_protocol


def test_protocol_requires_one_result_for_every_tool_call():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "x"}},
            {"id": "b", "function": {"name": "y"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "tool", "tool_call_id": "b", "content": "{}"},
    ]
    validate_message_protocol(messages)


def test_protocol_rejects_duplicate_empty_and_orphan_ids():
    with pytest.raises(MessageProtocolError):
        validate_message_protocol([{"role": "assistant", "content": "", "tool_calls": [
            {"id": "same", "function": {}}, {"id": "same", "function": {}}
        ]}], allow_pending_tail=True)
    with pytest.raises(MessageProtocolError):
        validate_message_protocol([{"role": "assistant", "content": "", "tool_calls": [
            {"id": "", "function": {}}
        ]}], allow_pending_tail=True)
    with pytest.raises(MessageProtocolError):
        validate_message_protocol([{"role": "tool", "tool_call_id": "orphan", "content": "{}"}])


def test_pending_tail_is_only_allowed_at_checkpoint_boundary():
    message = {"role": "assistant", "content": "", "tool_calls": [{"id": "pending", "function": {}}]}
    with pytest.raises(MessageProtocolError):
        validate_message_protocol([message])
    validate_message_protocol([message], allow_pending_tail=True)


def test_protocol_rejects_interleaving_user_or_system_before_tool_results():
    with pytest.raises(MessageProtocolError):
        validate_message_protocol([
            {"role": "assistant", "content": "", "tool_calls": [{"id": "pending", "function": {}}]},
            {"role": "system", "content": "dynamic context"},
        ], allow_pending_tail=True)
