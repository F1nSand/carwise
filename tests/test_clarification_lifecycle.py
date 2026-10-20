"""Clarification through the real HTTP worker, Agent loop and leased SQLite store."""

import copy
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.agent_runtime.loop as loop
from backend.agent_runtime.message_protocol import validate_message_protocol
from backend.agent_runtime.session_store import SessionStore
from backend.agent_runtime.sqlite_repository import SQLiteRuntimeRepository
from backend.service import create_app


def _call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(
        name=name, arguments=json.dumps(arguments, ensure_ascii=False),
    ))


def _response(calls):
    return SimpleNamespace(
        id="scripted-clarification",
        choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
            content="", reasoning_content=None, tool_calls=calls,
        ))],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


class ScriptedClient:
    """Only the provider is scripted; runtime tools and storage execute normally."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def is_available(self):
        return True

    def complete(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        assert self.responses, "Unexpected additional model round"
        return self.responses.pop(0)


def _clarification_script():
    steps = [
        {"id": "intent", "title": "澄清意图", "description": "确认问题指向",
         "tool_names": ["ask_user"], "goal_ids": ["g1"], "kind": "clarification",
         "completion_criteria": "user_answer"},
        {"id": "detail", "title": "确认说明范围", "description": "确认希望了解的内容",
         "tool_names": ["ask_user"], "goal_ids": ["g1"], "kind": "clarification",
         "completion_criteria": "user_answer"},
        {"id": "final", "title": "整理回答", "description": "说明助手能力",
         "tool_names": ["finalize_answer"], "goal_ids": ["g1"], "kind": "finalize",
         "completion_criteria": "validated_answer"},
    ]
    return [
        _response([
            _call("declare", "declare_task", {"requires_plan": True, "goals": [
                {"id": "g1", "description": "解释助手用途", "deliverable": "助手能力说明"},
            ]}),
            _call("plan", "set_plan", {"steps": steps}),
            _call("ask-intent", "ask_user", {"questions": [
                {"id": "intent", "prompt": "你想了解这个助手的用途吗？", "options": [],
                 "allow_custom": True, "required": True},
            ]}),
            _call("ask-detail", "ask_user", {"questions": [
                {"id": "detail", "prompt": "希望了解哪些能力？", "options": [],
                 "allow_custom": True, "required": True},
            ]}),
        ]),
        _response([_call("finalize", "finalize_answer", {
            "schema_version": 2, "result_kind": "explanation", "goal_ids": ["g1"],
            "blocks": [{"kind": "text", "text": "我是二手车分析助手，可以查询挂牌价格和车源，并解释数据的限制。"}],
            "limitation_codes": [],
        })]),
    ]


def _wait(client, run_id, *, tool_call_id=None, terminal=False):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200, response.text
        snapshot = response.json()
        if snapshot["status"] in {"done", "error", "cancelled"}:
            return snapshot
        if not terminal and snapshot["status"] == "waiting_user" and (
            tool_call_id is None or snapshot["pending_input"]["tool_call_id"] == tool_call_id
        ):
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"Run did not reach expected state: {snapshot}")


def test_vague_input_can_ask_twice_resume_same_batch_and_complete_without_losing_messages(tmp_path, monkeypatch):
    provider = ScriptedClient(_clarification_script())
    monkeypatch.setattr(loop, "get_client", lambda: provider)
    repository = SQLiteRuntimeRepository(tmp_path / "clarification.sqlite3")
    app = create_app(runtime_repository=repository)
    client = TestClient(app)
    try:
        submitted = client.post("/v1/runs", json={"input": "啥东西"})
        assert submitted.status_code == 202, submitted.text
        run_id = submitted.json()["run_id"]
        for question_id, value in [("intent", "想了解助手用途"), ("detail", "介绍整体能力")]:
            waiting = _wait(client, run_id, tool_call_id=f"ask-{question_id}")
            assert waiting["status"] == "waiting_user", waiting
            assert waiting.get("failure_code") is None
            assert waiting["pending_input"]["tool_call_id"] == f"ask-{question_id}"
            assert len(waiting["runtime"]["plan"]) == 3
            assert "_session_commit" not in waiting.get("result", {})
            stored = repository.get_run(run_id)
            session = repository.load_session(stored["context_id"])
            assert session.revision == stored["context_revision"]
            assert session.messages[0]["content"] == "啥东西"
            answered = client.post(f"/v1/runs/{run_id}/input", json={
                "tool_call_id": f"ask-{question_id}", "resume_revision": waiting["resume_revision"],
                "client_request_id": f"answer-{question_id}",
                "answers": [{"question_id": question_id, "value": value}],
            })
            assert answered.status_code == 202, answered.text

        done = _wait(client, run_id, terminal=True)
        assert done["status"] == "done", done
        assert "二手车分析助手" in done["result"]["summary"]
        assert "_session_commit" not in done["result"]
        assert all(step["status"] == "complete" for step in done["runtime"]["plan"])
        events = repository.read_run_events(run_id)
        assert not any(event["event_type"] == "error" for event in events)
        questions = [event for event in events if event["event_type"] == "input_required"]
        assert [event["pending_input"]["tool_call_id"] for event in questions] == ["ask-intent", "ask-detail"]
        assert len(provider.requests) == 2  # The second question resumes the existing tool batch.

        stored = repository.get_run(run_id)
        session = repository.load_session(stored["context_id"])
        assert session.revision == stored["context_revision"] == 3
        messages = [
            {key: value for key, value in record.items()
             if key in {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"}}
            for record in session.messages
        ]
        validate_message_protocol(messages)
        tool_ids = [message["tool_call_id"] for message in messages if message["role"] == "tool"]
        assert tool_ids == ["declare", "plan", "ask-intent", "ask-detail", "finalize"]
        assert sum(message.get("content") == "啥东西" for message in messages) == 1
        assert len({record["message_id"] for record in session.messages}) == len(session.messages)
    finally:
        app.state.scheduler.shutdown()


def test_resume_keeps_messages_saved_in_checkpoint_before_waiting_session_commit(tmp_path, monkeypatch):
    provider = ScriptedClient(_clarification_script())
    monkeypatch.setattr(loop, "get_client", lambda: provider)
    store = SessionStore(tmp_path / "sessions")
    saved = []

    def lose_worker_at_question(checkpoint):
        saved.append(copy.deepcopy(checkpoint))
        if checkpoint.get("pending_input"):
            raise RuntimeError("simulated_worker_loss_before_pause_commit")

    with pytest.raises(RuntimeError, match="simulated_worker_loss_before_pause_commit"):
        loop.run_agent("啥东西", session_id="recovery-session", run_id="recovery-run",
                       session_store=store, defer_session_commit=True, execution_attempt=1,
                       checkpoint_sink=lose_worker_at_question)
    assert store.load("recovery-session").messages == []
    checkpoint = saved[-1]
    assert checkpoint["pending_input"]["tool_call_id"] == "ask-intent"

    waiting = loop.run_agent(
        "啥东西", session_id="recovery-session", run_id="recovery-run", session_store=store,
        defer_session_commit=True, execution_attempt=2, resume_checkpoint=checkpoint,
        input_answers=[{"question_id": "intent", "value": "想了解助手用途"}],
    )
    assert waiting["status"] == "waiting_user", waiting
    assert waiting["state"]["pending_input"]["tool_call_id"] == "ask-detail"
    delta = waiting["_session_commit"]["messages"]
    assert delta[0] == {"role": "user", "content": "啥东西"}
    assert [message["tool_call_id"] for message in delta if message["role"] == "tool"] == [
        "declare", "plan", "ask-intent",
    ]
    assert not any(message["role"] == "system" for message in delta)


def test_cancelled_clarification_does_not_poison_next_run_in_same_context(tmp_path, monkeypatch):
    final_step = {"id": "final", "title": "说明助手用途", "description": "解释助手能力",
                  "tool_names": ["finalize_answer"], "goal_ids": ["g2"], "kind": "finalize",
                  "completion_criteria": "validated_answer"}
    provider = ScriptedClient([
        _clarification_script()[0],
        _response([
            _call("new-declare", "declare_task", {"requires_plan": True, "goals": [
                {"id": "g2", "description": "介绍助手", "deliverable": "能力说明"},
            ]}),
            _call("new-plan", "set_plan", {"steps": [final_step]}),
            _call("new-finalize", "finalize_answer", {
                "schema_version": 2, "result_kind": "explanation", "goal_ids": ["g2"],
                "blocks": [{"kind": "text", "text": "我是二手车分析助手，可以帮助查询和解释车源数据。"}],
                "limitation_codes": [],
            }),
        ]),
    ])
    monkeypatch.setattr(loop, "get_client", lambda: provider)
    repository = SQLiteRuntimeRepository(tmp_path / "cancel-clarification.sqlite3")
    app = create_app(runtime_repository=repository)
    client = TestClient(app)
    try:
        first = client.post("/v1/runs", json={"input": "啥东西"})
        run_id = first.json()["run_id"]
        waiting = _wait(client, run_id, tool_call_id="ask-intent")
        assert waiting["status"] == "waiting_user", waiting
        cancelled = client.post(f"/v1/runs/{run_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        submitted = client.post("/v1/runs", json={
            "input": "介绍你能做什么", "session_id": waiting["session_id"], "context_id": waiting["context_id"],
        })
        assert submitted.status_code == 202, submitted.text
        done = _wait(client, submitted.json()["run_id"], terminal=True)
        assert done["status"] == "done", done
        validate_message_protocol(provider.requests[-1]["messages"])
        messages = repository.load_session(waiting["context_id"]).messages
        validate_message_protocol(messages)
        interrupted = [message for message in messages
                       if message["role"] == "tool" and message["tool_call_id"] in {"ask-intent", "ask-detail"}]
        assert len(interrupted) == 2
        assert all(json.loads(message["content"])["ok"] is False for message in interrupted)
    finally:
        app.state.scheduler.shutdown()
