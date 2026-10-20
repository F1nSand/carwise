import threading
import time

from fastapi.testclient import TestClient

from backend.agent_runtime.sqlite_repository import SQLiteRuntimeRepository
from backend.auth import owner_id_from_cookie
from backend.service import create_app


def test_conversation_create_list_detail_entries_and_owner_scope(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-api.sqlite3")
    secret = "conversation-api-owner-secret"
    client = TestClient(create_app(runtime_repository=repository, owner_cookie_secret=secret))
    created = client.post("/v1/conversations", json={"client_request_id": "create-chat-1"})
    assert created.status_code == 201
    body = created.json()
    assert body["conversation_id"] == body["context_id"]
    replay = client.post("/v1/conversations", json={"client_request_id": "create-chat-1"})
    assert replay.status_code == 201
    assert replay.json()["conversation_id"] == body["conversation_id"]

    owner_id = owner_id_from_cookie(client.cookies.get("uca_owner"), secret)
    repository.append_conversation_entry(body["conversation_id"], "user_message", "client:one", {"text": "问题一"})
    repository.append_conversation_entry(body["conversation_id"], "assistant_message", "run:one:answer", {"text": "回答一"})
    listed = client.get("/v1/conversations")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["conversation_id"] == body["conversation_id"]
    detail = client.get(f"/v1/conversations/{body['conversation_id']}")
    assert detail.status_code == 200
    assert detail.json()["conversation_revision"] == 2
    entries = client.get(f"/v1/conversations/{body['conversation_id']}/entries")
    assert entries.status_code == 200
    assert entries.json()["high_water_seq"] == 2
    assert [item["kind"] for item in entries.json()["entries"]] == ["user_message", "assistant_message"]

    foreign = TestClient(client.app)
    assert foreign.get(f"/v1/conversations/{body['conversation_id']}").status_code == 404
    assert foreign.get(f"/v1/conversations/{body['conversation_id']}/entries").status_code == 404
    assert owner_id


def test_conversation_create_requires_idempotency_key(tmp_path):
    client = TestClient(create_app(
        runtime_repository=SQLiteRuntimeRepository(tmp_path / "conversation-api-invalid.sqlite3"),
        owner_cookie_secret="conversation-api-owner-secret",
    ))
    assert client.post("/v1/conversations", json={}).status_code == 400


def test_conversation_message_route_replays_the_same_acceptance_and_keeps_owner_scope(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-message-api.sqlite3")
    client = TestClient(create_app(runtime_repository=repository, owner_cookie_secret="conversation-message-owner"))
    conversation_id = client.post("/v1/conversations", json={"client_request_id": "create-chat"}).json()["conversation_id"]
    payload = {"client_request_id": "message-1", "content": "推荐 15 万内 SUV"}

    accepted = client.post(f"/v1/conversations/{conversation_id}/messages", json=payload)
    replay = client.post(f"/v1/conversations/{conversation_id}/messages", json=payload)

    assert accepted.status_code == 202
    assert accepted.json() == replay.json()
    assert accepted.json()["accepted_kind"] == "run"
    assert accepted.json()["entry"]["payload"]["text"] == payload["content"]
    entries = client.get(f"/v1/conversations/{conversation_id}/entries").json()["entries"]
    assert len(entries) == 1
    foreign = TestClient(client.app)
    assert foreign.post(f"/v1/conversations/{conversation_id}/messages", json=payload).status_code == 404


def test_conversation_acceptance_uses_same_durable_pending_admission(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-admission.sqlite3")
    app = create_app(runtime_repository=repository, owner_cookie_secret="conversation-admission",
                     admission_limits={"global_pending": 1})
    # Hold accepted work queued: a fast offline runner may otherwise finish
    # before the second request and correctly free its pending slot.
    app.state.scheduler.submit = lambda *_args: None
    client = TestClient(app)
    first_id = client.post("/v1/conversations", json={"client_request_id": "chat-a"}).json()["conversation_id"]
    second_id = client.post("/v1/conversations", json={"client_request_id": "chat-b"}).json()["conversation_id"]
    payload = {"client_request_id": "message-a", "content": "第一条"}
    accepted = client.post(f"/v1/conversations/{first_id}/messages", json=payload)
    replay = client.post(f"/v1/conversations/{first_id}/messages", json=payload)
    rejected = client.post(f"/v1/conversations/{second_id}/messages",
                           json={"client_request_id": "message-b", "content": "第二条"})
    assert accepted.status_code == replay.status_code == 202
    assert accepted.json() == replay.json()
    assert rejected.status_code == 429
    assert repository.list_conversation_entries_owned(second_id,
        owner_id_from_cookie(client.cookies.get("uca_owner"), "conversation-admission"))["entries"] == []


def test_conversation_message_route_steers_the_active_run_once(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.service.MIDRUN_INPUT_ENABLED", True)
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-message-steer-api.sqlite3")
    client = TestClient(create_app(runtime_repository=repository, owner_cookie_secret="conversation-steer-owner"))
    conversation_id = client.post("/v1/conversations", json={"client_request_id": "create-chat"}).json()["conversation_id"]
    owner_id = owner_id_from_cookie(client.cookies.get("uca_owner"), "conversation-steer-owner")
    repository.create_run("run-active-api", "browser-group", "原问题", context_id=conversation_id, owner_id=owner_id)
    lease = repository.acquire_run_lease("run-active-api", "test-worker", lease_ttl_s=10)
    assert lease

    payload = {"client_request_id": "steer-api-1", "content": "改成杭州"}
    accepted = client.post(f"/v1/conversations/{conversation_id}/messages", json=payload)
    replay = client.post(f"/v1/conversations/{conversation_id}/messages", json=payload)

    assert accepted.status_code == 202
    assert accepted.json() == replay.json()
    assert accepted.json()["accepted_kind"] == "steering"
    assert accepted.json()["run_id"] == "run-active-api"
    assert repository.get_run("run-active-api")["runtime"]["input_revision"] == 1
    assert len(repository.list_conversation_entries_owned(conversation_id, owner_id)["entries"]) == 1


def test_http_steering_accepted_at_completion_boundary_reuses_run_and_invalidates_stale_answer(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.service.MIDRUN_INPUT_ENABLED", True)
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-completion-race-api.sqlite3")
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs["run_id"])
        checkpoint = {"messages": [{"role": "user", "content": kwargs["user_text"]}],
                      "flags": {"steps": len(calls)}, "run_logs": [], "plan": []}
        kwargs["checkpoint_sink"](checkpoint)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
            answer = "旧条件答案"
        else:
            pending = kwargs["message_source"]()
            assert pending
            assert kwargs["message_ack"](pending, checkpoint)
            answer = f"已按补充条件调整：{pending[0]['content']}"
        return {
            "status": "done", "summary": answer,
            "final_message": {"role": "assistant", "content": answer, "source": "validated"},
            "report": {"id": kwargs["run_id"], "summaryTitle": "对话报告", "summaryText": answer, "answer": answer},
            "_session_commit": {"messages": [{"role": "user", "content": kwargs["user_text"]}],
                                "run_id": kwargs["run_id"]},
        }

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner,
                                   owner_cookie_secret="completion-race-owner"))
    conversation_id = client.post("/v1/conversations", json={"client_request_id": "create-race-chat"}).json()["conversation_id"]
    first = client.post(f"/v1/conversations/{conversation_id}/messages", json={
        "client_request_id": "first-chat-message", "content": "推荐 15 万内的车",
    })
    assert first.status_code == 202
    assert entered.wait(1)
    steering = client.post(f"/v1/conversations/{conversation_id}/messages", json={
        "client_request_id": "race-steering-message", "content": "补充：预算改成 18 万",
    })
    assert steering.status_code == 202
    assert steering.json()["accepted_kind"] == "steering"
    release.set()

    run_id = first.json()["run_id"]
    for _ in range(300):
        snapshot = repository.get_run(run_id)
        if snapshot["status"] in {"done", "error"}:
            break
        time.sleep(0.01)
    assert snapshot["status"] == "done"
    assert snapshot["result"]["summary"] == "已按补充条件调整：补充：预算改成 18 万"
    assert calls == [run_id, run_id]
    timeline = repository.list_conversation_entries_owned(conversation_id, snapshot["owner_id"])["entries"]
    kinds = [entry["kind"] for entry in timeline]
    assert kinds[:2] == ["user_message", "user_message"]
    assert kinds[-2:] == ["assistant_message", "report_card"]
    assert kinds[2:-2] in ([], ["execution_group"])
    assert not any(entry["payload"].get("text") == "旧条件答案" for entry in timeline)


def test_report_source_selection_reaches_the_next_run_and_creates_a_child_version(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "conversation-source-report-api.sqlite3")
    calls = []

    def fake_runner(**kwargs):
        calls.append(kwargs)
        answer = f"已完成第 {len(calls)} 次分析"
        return {
            "status": "done", "summary": answer,
            "final_message": {"role": "assistant", "content": answer, "source": "validated"},
            "report": {"id": kwargs["run_id"], "summaryTitle": "分析报告", "summaryText": answer, "answer": answer},
            "_session_commit": {"messages": [{"role": "user", "content": kwargs["user_text"]}],
                                "run_id": kwargs["run_id"]},
        }

    client = TestClient(create_app(runtime_repository=repository, runner=fake_runner,
                                   owner_cookie_secret="source-report-owner"))
    conversation_id = client.post("/v1/conversations", json={"client_request_id": "create-source-chat"}).json()["conversation_id"]

    def send(payload):
        response = client.post(f"/v1/conversations/{conversation_id}/messages", json=payload)
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        for _ in range(200):
            snapshot = repository.get_run(run_id)
            if snapshot["status"] in {"done", "error"}:
                break
            time.sleep(0.01)
        assert snapshot["status"] == "done"
        return snapshot

    first = send({"client_request_id": "source-first", "content": "分析 15 万预算"})
    report_id = first["result"]["report_ref"]["report_id"]
    second = send({"client_request_id": "source-second", "content": "改成 18 万预算", "source_report_id": report_id})

    assert calls[1]["source_report_id"] == report_id
    assert second["result"]["report_ref"]["parent_report_id"] == report_id
    assert second["result"]["report_ref"]["version"] == 2
