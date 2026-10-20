import threading
import time

from backend.agent_runtime.scheduler import RunScheduler
from backend.agent_runtime.sqlite_repository import SQLiteRuntimeRepository


def test_scheduler_enforces_worker_and_queue_bounds_without_orphaning_runs(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")
    for index in range(6):
        repository.create_run(
            f"run-{index}", "session-bounded", "问题", context_id=f"context-bounded-{index}", owner_id="owner-a",
        )
    release = threading.Event()
    started = []
    started_guard = threading.Lock()

    def worker(lease):
        with started_guard:
            started.append(lease.run_id)
        release.wait(2)
        repository.finish_run(lease.run_id, {"summary": "done"}, lease_token=lease.token)

    scheduler = RunScheduler(
        repository=repository,
        worker=worker,
        max_concurrency=2,
        max_queue_size=3,
        heartbeat_interval_s=0.01,
        lease_ttl_s=0.2,
    )
    scheduler.start()
    results = [scheduler.submit(f"run-{index}") for index in range(6)]

    assert [item.accepted for item in results] == [True, True, True, True, True, True]
    deadline = time.monotonic() + 1
    while len(started) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(started) == 2
    stats = scheduler.stats()
    assert stats.active_count == 2
    assert stats.queue_depth == 3
    assert repository.get_run("run-5")["status"] == "queued"

    release.set()
    scheduler.shutdown(grace_seconds=1)
    assert all(repository.get_run(f"run-{index}")["status"] == "done" for index in range(6))


def test_resume_submitted_while_paused_worker_is_exiting_is_redispatched(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "resume-race.sqlite3")
    repository.create_run("run-resume-race", "session-resume-race", "请问预算", owner_id="owner-a")
    paused = threading.Event()
    release_old_worker = threading.Event()
    attempts = []

    def worker(lease):
        attempts.append(lease.attempt)
        if lease.attempt == 1:
            pending = {"tool_call_id": "ask-race", "questions": [{"id": "budget"}]}
            assert repository.pause_run(
                lease.run_id,
                {"pending_input": pending},
                pending,
                waiting_event={"event_type": "input_required", "pending_input": pending},
                lease_token=lease.token,
            ) is not None
            paused.set()
            assert release_old_worker.wait(1)
            return
        repository.finish_run(lease.run_id, {"summary": "完成"}, lease_token=lease.token)

    scheduler = RunScheduler(
        repository=repository,
        worker=worker,
        max_concurrency=1,
        max_queue_size=1,
        heartbeat_interval_s=0.01,
        lease_ttl_s=1,
    )
    assert scheduler.submit("run-resume-race").accepted
    assert paused.wait(1)

    snapshot = repository.get_run("run-resume-race")
    assert repository.prepare_run_resume(
        "run-resume-race",
        {"resume_requested": True, "input_answers": [{"question_id": "budget", "value": "15"}]},
        expected_tool_call_id="ask-race",
        expected_revision=snapshot["resume_revision"],
    )
    assert scheduler.submit("run-resume-race").accepted
    release_old_worker.set()

    deadline = time.monotonic() + 2
    while repository.get_run("run-resume-race")["status"] != "done" and time.monotonic() < deadline:
        time.sleep(0.005)
    scheduler.shutdown(grace_seconds=1)

    assert attempts == [1, 2]
    assert repository.get_run("run-resume-race")["status"] == "done"


def test_runs_sharing_a_context_are_serialized_in_acceptance_order(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "context-serial.sqlite3")
    repository.create_run(
        "run-context-1", "history-context", "first", context_id="shared-context", owner_id="owner-a",
    )
    repository.create_run(
        "run-context-2", "history-context", "second", context_id="shared-context", owner_id="owner-a",
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    guard = threading.Lock()
    active = 0
    max_active = 0
    started = []

    def worker(lease):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
            started.append(lease.run_id)
        if lease.run_id == "run-context-1":
            first_entered.set()
            assert release_first.wait(1)
        repository.finish_run(lease.run_id, {"summary": "done"}, lease_token=lease.token)
        with guard:
            active -= 1

    scheduler = RunScheduler(
        repository=repository,
        worker=worker,
        max_concurrency=2,
        max_queue_size=2,
        heartbeat_interval_s=0.01,
        lease_ttl_s=1,
    )
    assert scheduler.submit("run-context-1").accepted
    assert scheduler.submit("run-context-2").accepted
    assert first_entered.wait(1)
    time.sleep(0.05)
    assert started == ["run-context-1"]
    release_first.set()

    deadline = time.monotonic() + 2
    while repository.get_run("run-context-2")["status"] != "done" and time.monotonic() < deadline:
        time.sleep(0.005)
    scheduler.shutdown(grace_seconds=1)

    assert started == ["run-context-1", "run-context-2"]
    assert max_active == 1
    assert repository.get_run("run-context-1")["status"] == "done"
    assert repository.get_run("run-context-2")["status"] == "done"


def test_scheduler_reconciles_queued_waiting_and_lost_running_runs(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")
    repository.create_run("run-queued", "session-reconcile", "排队", context_id="context-queued", owner_id="owner-a")
    repository.create_run("run-waiting", "session-reconcile", "等待", context_id="context-waiting", owner_id="owner-a")
    waiting_lease = repository.acquire_run_lease("run-waiting", "worker-1", lease_ttl_s=10)
    repository.pause_run(
        "run-waiting",
        {"pending_input": {"tool_call_id": "ask-1"}},
        {"tool_call_id": "ask-1", "questions": [{"id": "budget"}]},
        lease_token=waiting_lease["token"],
    )
    repository.create_run("run-lost", "session-reconcile", "丢失", context_id="context-lost", owner_id="owner-a")
    lost_lease = repository.acquire_run_lease("run-lost", "worker-lost", lease_ttl_s=1)

    changed = repository.reconcile_runs(now_iso=lost_lease["expires_at"])

    assert repository.get_run("run-queued")["status"] == "queued"
    assert repository.get_run("run-waiting")["status"] == "waiting_user"
    lost = repository.get_run("run-lost")
    assert lost["status"] == "error"
    assert lost["failure_code"] == "worker_lost"
    assert lost["retryable"] is True
    assert any(item["run_id"] == "run-lost" for item in changed)


def test_expired_lease_is_fenced_and_explicit_retry_gets_a_new_lease(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")
    repository.create_run("run-fenced", "session-fenced", "问题", owner_id="owner-a")
    first = repository.acquire_run_lease("run-fenced", "worker-a", lease_ttl_s=0.02)
    time.sleep(0.04)

    assert repository.heartbeat_run("run-fenced", first["token"], lease_ttl_s=1) is False
    repository.reconcile_runs()
    assert repository.get_run("run-fenced")["failure_code"] == "worker_lost"
    assert repository.append_run_event("run-fenced", {"event_type": "late"}, lease_token=first["token"]) is None

    repository.create_run("run-retry", "session-fenced", "问题", owner_id="owner-a")
    second = repository.acquire_run_lease("run-retry", "worker-b", lease_ttl_s=1)
    assert second["token"] != first["token"]
    assert second["attempt"] == 1


def test_lease_queue_wait_uses_latest_enqueue_time_not_original_run_creation(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")
    repository.create_run("run-queue-wait", "session-queue-wait", "问题", owner_id="owner-a")
    connection = repository._connect()
    try:
        connection.execute(
            "UPDATE runs SET created_at=? WHERE run_id=?",
            ("2000-01-01T00:00:00+00:00", "run-queue-wait"),
        )
        connection.commit()
    finally:
        connection.close()

    lease = repository.acquire_run_lease("run-queue-wait", "worker-queue", lease_ttl_s=1)

    assert lease["queue_wait_ms"] < 2_000


def test_shutdown_stops_intake_and_marks_unfinished_worker_interrupted(tmp_path):
    repository = SQLiteRuntimeRepository(tmp_path / "runtime.sqlite3")
    repository.create_run("run-blocked", "session-shutdown", "问题", owner_id="owner-a")
    entered = threading.Event()
    release = threading.Event()

    def worker(lease):
        entered.set()
        release.wait(2)
        try:
            repository.finish_run(lease.run_id, {"summary": "late"}, lease_token=lease.token)
        except (KeyError, RuntimeError):
            pass

    scheduler = RunScheduler(
        repository=repository,
        worker=worker,
        max_concurrency=1,
        max_queue_size=1,
        heartbeat_interval_s=0.01,
        lease_ttl_s=0.2,
    )
    scheduler.start()
    assert scheduler.submit("run-blocked").accepted is True
    assert entered.wait(1)
    scheduler.shutdown(grace_seconds=0.01)

    assert scheduler.submit("run-blocked").accepted is False
    interrupted = repository.get_run("run-blocked")
    assert interrupted["status"] == "error"
    assert interrupted["failure_code"] == "shutdown_interrupted"
    assert interrupted["retryable"] is True
    release.set()


def test_dispatch_failure_is_visible_and_durable_run_executes_once_after_recovery(tmp_path, monkeypatch):
    repository = SQLiteRuntimeRepository(tmp_path / "dispatch-health.sqlite3")
    repository.create_run("dispatch-health", "dispatch-session", "offline", owner_id="owner")
    calls = []
    def worker(lease):
        calls.append(lease.run_id)
        repository.finish_run(lease.run_id, result={"summary": "offline"}, lease_token=lease.token)
    scheduler = RunScheduler(repository=repository, worker=worker, heartbeat_interval_s=0.01)
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("worker pool unavailable")
    monkeypatch.setattr(scheduler._executor, "submit", unavailable)
    scheduler.start()
    try:
        for _ in range(100):
            if not scheduler.stats().healthy:
                break
            time.sleep(0.005)
        assert scheduler.stats().healthy is False
        assert repository.get_run("dispatch-health")["status"] == "queued"
        assert calls == []
        monkeypatch.undo()
        for _ in range(200):
            if repository.get_run("dispatch-health")["status"] == "done":
                break
            time.sleep(0.005)
        assert repository.get_run("dispatch-health")["status"] == "done"
        assert calls == ["dispatch-health"]
        assert scheduler.stats().healthy is True
    finally:
        scheduler.shutdown(grace_seconds=0)


def test_initial_durable_scan_failure_does_not_disable_dispatcher(tmp_path, monkeypatch):
    repository = SQLiteRuntimeRepository(tmp_path / "startup-scan.sqlite3")
    repository.create_run("startup-scan", "startup-session", "offline", owner_id="owner")
    original = repository.list_runs
    attempts = []
    def flaky_scan(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("temporary scan failure")
        return original(*args, **kwargs)
    monkeypatch.setattr(repository, "list_runs", flaky_scan)
    def worker(lease):
        repository.finish_run(lease.run_id, result={"summary": "offline"}, lease_token=lease.token)
    scheduler = RunScheduler(repository=repository, worker=worker, heartbeat_interval_s=0.01)
    scheduler.start()
    try:
        for _ in range(200):
            if repository.get_run("startup-scan")["status"] == "done":
                break
            time.sleep(0.005)
        assert repository.get_run("startup-scan")["status"] == "done"
        assert len(attempts) >= 2
        assert scheduler.stats().healthy
    finally:
        scheduler.shutdown(grace_seconds=0)
