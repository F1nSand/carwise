import json

import pytest

from backend.agent_runtime.session_store import SessionStore


def test_session_store_persists_active_messages_and_survives_reload(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    first = store.append_messages(
        "demo-1",
        [
            {"role": "user", "content": "预算 15 万"},
            {"role": "assistant", "content": "记住预算了"},
        ],
        run_id="run-1",
    )

    reloaded = SessionStore(tmp_path / "sessions")
    assert [item["role"] for item in reloaded.active_messages("demo-1")] == ["user", "assistant"]
    assert reloaded.load("demo-1").active_head_id == first.active_head_id
    payload = json.loads((tmp_path / "sessions" / "demo-1.json").read_text(encoding="utf-8"))
    assert "api_key" not in json.dumps(payload, ensure_ascii=False).lower()


def test_session_store_rejects_path_like_ids(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    with pytest.raises(ValueError, match="session_id"):
        store.load("..\\outside")


def test_rollback_moves_active_head_but_keeps_append_only_history(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    snapshot = store.append_messages(
        "demo-2",
        [
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "第一轮答复"},
            {"role": "user", "content": "第二轮"},
            {"role": "assistant", "content": "第二轮答复"},
        ],
        run_id="run-2",
    )
    target = snapshot.messages[2]["message_id"]

    rolled = store.rollback("demo-2", target)

    assert [item["content"] for item in store.active_messages("demo-2")] == ["第一轮", "第一轮答复"]
    assert len(rolled.messages) == 4
    assert rolled.revision == snapshot.revision + 1


def test_append_same_run_id_is_idempotent(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    messages = [{"role": "user", "content": "只保留一次"}]

    first = store.append_messages("demo-3", messages, run_id="run-3")
    second = store.append_messages("demo-3", messages, run_id="run-3")

    assert len(first.messages) == len(second.messages) == 1
    assert len(store.active_messages("demo-3")) == 1


def test_session_store_rejects_system_messages_as_dynamic_history(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    with pytest.raises(ValueError, match="system"):
        store.append_messages("demo-4", [{"role": "system", "content": "not dynamic"}], run_id="run-4")
