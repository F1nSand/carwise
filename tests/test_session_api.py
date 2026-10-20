import time

from fastapi.testclient import TestClient

from backend.agent_runtime.run_store import RunStore
from backend.agent_runtime.session_store import SessionStore
from backend.auth import owner_id_from_cookie
from backend.service import create_app


def test_rollback_endpoint_moves_head_and_reports_hidden_branch(tmp_path):
    sessions = SessionStore(tmp_path / "sessions")
    secret = "session-api-owner-secret"
    client = TestClient(
        create_app(
            run_store=RunStore(tmp_path / "runs"),
            session_store=sessions,
            owner_cookie_secret=secret,
        )
    )
    client.get("/v1/history", params={"session_id": "bootstrap"})
    owner_id = owner_id_from_cookie(client.cookies.get("uca_owner"), secret)
    assert owner_id
    snapshot = sessions.append_messages(
        "session-rollback",
        [
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "第一轮答复"},
            {"role": "user", "content": "第二轮"},
        ],
        run_id="run-history",
        owner_id=owner_id,
    )
    target_id = snapshot.messages[2]["message_id"]

    response = client.post(
        "/v1/sessions/session-rollback/rollback",
        json={"message_id": target_id, "expected_revision": snapshot.revision},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["hidden_message_count"] == 1
    assert [item["content"] for item in body["messages"]] == ["第一轮", "第一轮答复"]
    assert len(sessions.load("session-rollback").messages) == 3
    other_owner = TestClient(client.app)
    assert other_owner.post(
        "/v1/sessions/session-rollback/rollback",
        json={"message_id": target_id},
    ).status_code == 404


def test_rollback_rejects_stale_revision(tmp_path):
    sessions = SessionStore(tmp_path / "sessions")
    secret = "session-api-owner-secret"
    client = TestClient(
        create_app(
            run_store=RunStore(tmp_path / "runs"),
            session_store=sessions,
            owner_cookie_secret=secret,
        )
    )
    client.get("/v1/history", params={"session_id": "bootstrap"})
    owner_id = owner_id_from_cookie(client.cookies.get("uca_owner"), secret)
    assert owner_id
    snapshot = sessions.append_messages(
        "session-revision", [{"role": "user", "content": "问题"}], run_id="run-revision", owner_id=owner_id
    )
    target_id = snapshot.messages[0]["message_id"]
    sessions.rollback("session-revision", target_id, owner_id=owner_id)

    response = client.post(
        "/v1/sessions/session-revision/rollback",
        json={"message_id": target_id, "expected_revision": snapshot.revision},
    )

    assert response.status_code == 409


def test_failed_run_can_retry_without_resubmitting_user_message(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")
    seen: list[bool] = []
    fail_once = {"value": True}

    def fake_runner(**kwargs):
        seen.append(bool(kwargs.get("reuse_current_user")))
        if fail_once["value"]:
            fail_once["value"] = False
            raise RuntimeError("temporary model failure")
        kwargs["event_sink"]({"event_type": "final", "step": 1})
        return {"summary": "retry ok"}

    client = TestClient(create_app(run_store=runs, session_store=sessions, runner=fake_runner))
    first = client.post(
        "/v1/runs",
        json={"run_id": "run-failed", "session_id": "session-retry", "input": "预算 15 万"},
    )
    assert first.status_code == 202
    for _ in range(50):
        if client.get("/v1/runs/run-failed").json()["status"] == "error":
            break
        time.sleep(0.01)
    assert client.get("/v1/runs/run-failed").json()["status"] == "error"

    other_owner = TestClient(client.app)
    assert other_owner.post("/v1/runs/run-failed/retry").status_code == 404
    retry = client.post("/v1/runs/run-failed/retry")
    assert retry.status_code == 202
    retry_id = retry.json()["run_id"]
    for _ in range(50):
        if client.get(f"/v1/runs/{retry_id}").json()["status"] == "done":
            break
        time.sleep(0.01)
    assert client.get(f"/v1/runs/{retry_id}").json()["status"] == "done"
    assert seen == [False, True]
