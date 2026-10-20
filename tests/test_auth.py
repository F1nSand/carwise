import time

from fastapi.testclient import TestClient

from backend.agent_runtime.run_store import RunStore
from backend.agent_runtime.session_store import SessionStore
from backend.auth import create_owner_cookie, owner_id_from_cookie, renew_owner_cookie, verify_owner_cookie
from backend.service import create_app
import pytest
import backend.auth as auth_module


def test_owner_cookie_is_signed_and_expires():
    cookie = create_owner_cookie("unit-test-secret", now=1_000)

    assert owner_id_from_cookie(cookie, "unit-test-secret", now=1_001) is not None
    assert verify_owner_cookie(cookie, "unit-test-secret", now=1_001)
    assert owner_id_from_cookie(cookie, "wrong-secret", now=1_001) is None
    assert owner_id_from_cookie(cookie + "x", "unit-test-secret", now=1_001) is None
    assert owner_id_from_cookie(cookie, "unit-test-secret", now=1_000 + 2_592_001) is None


def test_sliding_cookie_renewal_preserves_owner_and_resets_age():
    original = create_owner_cookie("unit-test-secret", now=1_000)
    renewed = renew_owner_cookie(original, "unit-test-secret", now=2_000, max_age_s=1_500)
    assert renewed is not None
    assert owner_id_from_cookie(original, "unit-test-secret", now=2_000, max_age_s=1_500) == owner_id_from_cookie(
        renewed, "unit-test-secret", now=2_000, max_age_s=1_500
    )
    assert owner_id_from_cookie(renewed, "unit-test-secret", now=3_501, max_age_s=1_500) is None
    assert renew_owner_cookie(original, "unit-test-secret", now=2_501, max_age_s=1_500) is None


def test_principal_resolver_refreshes_cookie_for_the_same_owner(monkeypatch, tmp_path):
    now = [10_000]
    monkeypatch.setattr(auth_module.time, "time", lambda: now[0])
    client = TestClient(create_app(run_store=RunStore(tmp_path / "renew-runs"),
                                   session_store=SessionStore(tmp_path / "renew-sessions"),
                                   owner_cookie_secret="renew-secret", owner_cookie_max_age_s=100))
    first = client.get("/v1/history", params={"session_id": "renew"})
    original = client.cookies.get("uca_owner")
    original_owner = owner_id_from_cookie(original, "renew-secret", now=now[0], max_age_s=100)
    now[0] += 50
    renewed_response = client.get("/v1/history", params={"session_id": "renew"})
    renewed = client.cookies.get("uca_owner")
    assert first.status_code == renewed_response.status_code == 200
    assert renewed != original
    assert owner_id_from_cookie(renewed, "renew-secret", now=now[0], max_age_s=100) == original_owner


def test_run_and_session_routes_are_isolated_by_owner_cookie(tmp_path):
    runs = RunStore(tmp_path / "runs")
    sessions = SessionStore(tmp_path / "sessions")

    def fake_runner(**kwargs):
        kwargs["session_store"].append_messages(
            kwargs["context_id"],
            [{"role": "user", "content": kwargs["user_text"]}, {"role": "assistant", "content": "可见结果"}],
            run_id=kwargs["run_id"],
            owner_id=kwargs["owner_id"],
        )
        return {"summary": "完成", "report": {"id": kwargs["run_id"], "intent": "unknown"}}

    app = create_app(
        run_store=runs,
        session_store=sessions,
        runner=fake_runner,
        owner_cookie_secret="route-test-secret",
    )
    owner_a = TestClient(app)
    owner_b = TestClient(app)
    created = owner_a.post(
        "/v1/runs",
        json={"run_id": "run-owned", "session_id": "session-owned", "input": "车价问题"},
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    owner_cookie = owner_a.cookies.get("uca_owner")
    owner_id = owner_id_from_cookie(owner_cookie, "route-test-secret")
    assert owner_id

    for _ in range(100):
        if owner_a.get(f"/v1/runs/{run_id}").json()["status"] == "done":
            break
        time.sleep(0.005)

    assert owner_a.get(f"/v1/runs/{run_id}").status_code == 200
    assert owner_a.get(f"/v1/runs/{run_id}/events").status_code == 200
    assert owner_a.get("/v1/history", params={"session_id": "session-owned"}).status_code == 200
    assert owner_a.get("/v1/sessions/session-owned/messages").status_code == 200

    assert owner_b.get(f"/v1/runs/{run_id}").status_code == 404
    assert owner_b.get(f"/v1/runs/{run_id}/events").status_code == 404
    assert owner_b.post(f"/v1/runs/{run_id}/input", json={"answers": "malformed"}).status_code == 404
    assert owner_b.post(f"/v1/runs/{run_id}/retry").status_code == 404
    assert owner_b.get("/v1/history", params={"session_id": "session-owned"}).json() == []
    assert owner_b.get("/v1/sessions/session-owned/messages").status_code == 404

    tampered = TestClient(app)
    tampered.cookies.set("uca_owner", f"{owner_cookie}tampered")
    denied = tampered.get(f"/v1/runs/{run_id}")
    assert denied.status_code == 404
    assert "uca_owner=" in denied.headers.get("set-cookie", "")

    session_snapshot = sessions.load_owned("session-owned", owner_id)
    message_id = session_snapshot.messages[0]["message_id"]
    rolled_back = owner_a.post(
        "/v1/sessions/session-owned/rollback",
        json={"message_id": message_id, "expected_revision": session_snapshot.revision},
    )
    assert rolled_back.status_code == 200
    assert owner_b.post(
        "/v1/sessions/session-owned/rollback",
        json={"message_id": message_id, "expected_revision": session_snapshot.revision},
    ).status_code == 404


def test_owner_cookie_attributes_and_configurable_secure_flag(tmp_path):
    client = TestClient(
        create_app(
            run_store=RunStore(tmp_path / "runs"),
            session_store=SessionStore(tmp_path / "sessions"),
            owner_cookie_secret="cookie-test-secret",
            owner_cookie_secure=True,
        )
    )
    response = client.get("/v1/history", params={"session_id": "session-new"})

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "uca_owner=" in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "max-age=2592000" in cookie
    assert "secure" in cookie


def test_production_requires_owner_cookie_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("OWNER_COOKIE_SECRET", raising=False)

    try:
        create_app(run_store=RunStore(tmp_path / "runs"), session_store=SessionStore(tmp_path / "sessions"))
    except RuntimeError as exc:
        assert "OWNER_COOKIE_SECRET" in str(exc)
    else:
        raise AssertionError("production app must reject a missing owner-cookie secret")

    monkeypatch.setenv("OWNER_COOKIE_SECRET", "production-test-secret")
    try:
        create_app(
            run_store=RunStore(tmp_path / "runs-secure"),
            session_store=SessionStore(tmp_path / "sessions-secure"),
            owner_cookie_secure=False,
        )
    except RuntimeError as exc:
        assert "OWNER_COOKIE_SECURE" in str(exc)
    else:
        raise AssertionError("production app must require Secure cookies")


def test_stores_keep_owner_immutable_and_reject_ownerless_legacy_records(tmp_path):
    runs = RunStore(tmp_path / "runs")
    run = runs.create("run-owner", "session-owner", "问题", owner_id="owner-a")
    assert run["owner_id"] == "owner-a"
    assert runs.load_owned("run-owner", "owner-a")["run_id"] == "run-owner"
    assert runs.list_owned("owner-a", "session-owner")[0]["run_id"] == "run-owner"
    assert runs.list_owned("owner-b") == []
    with pytest.raises(KeyError):
        runs.load_owned("run-owner", "owner-b")
    with pytest.raises(KeyError):
        runs.create("run-owner", "session-owner", "问题", owner_id="owner-b")

    legacy = runs.create("run-ownerless", "session-owner", "旧记录")
    with pytest.raises(KeyError):
        runs.load_owned(legacy["run_id"], "owner-a")

    sessions = SessionStore(tmp_path / "sessions")
    sessions.append_messages(
        "session-owner",
        [{"role": "user", "content": "问题"}],
        run_id="run-owner",
        owner_id="owner-a",
    )
    assert sessions.load_owned("session-owner", "owner-a").owner_id == "owner-a"
    with pytest.raises(KeyError):
        sessions.load_owned("session-owner", "owner-b")
    with pytest.raises(KeyError):
        sessions.append_messages(
            "session-owner", [{"role": "user", "content": "越权"}], run_id="run-other", owner_id="owner-b"
        )
    sessions.append_messages("session-legacy", [{"role": "user", "content": "旧记录"}])
    with pytest.raises(KeyError):
        sessions.load_owned("session-legacy", "owner-a")
