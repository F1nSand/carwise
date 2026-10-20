"""Bounded in-process runner with persisted SQLite leases and fencing."""

from __future__ import annotations

import os
import socket
import threading
import time
import logging
from datetime import datetime, timezone
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4


logger = logging.getLogger(__name__)


def _elapsed_since_ms(value: Any) -> int | None:
    try:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - timestamp).total_seconds() * 1000))
    except (TypeError, ValueError):
        return None


@dataclass
class RunLease:
    run_id: str
    worker_id: str
    token: str | None
    expires_at: str | None
    attempt: int
    queue_wait_ms: int | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    started_monotonic: float = field(default_factory=time.monotonic)
    lease_deadline_monotonic: float = 0.0


@dataclass(frozen=True)
class SubmitResult:
    accepted: bool
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class SchedulerStats:
    active_count: int
    queue_depth: int
    accepting: bool
    max_concurrency: int
    max_queue_size: int
    healthy: bool = True


class RunScheduler:
    """Run accepted work with a fixed worker pool and a bounded waiting queue.

    SQLite is the production path. An injected JSON RunStore remains supported
    for service tests and local compatibility, but has only process-local claims.
    """

    def __init__(
        self,
        *,
        repository: Any | None,
        worker: Callable[[RunLease], Any],
        store: Any | None = None,
        max_concurrency: int = 4,
        max_queue_size: int = 32,
        run_timeout_s: float = 180.0,
        heartbeat_interval_s: float = 5.0,
        lease_ttl_s: float = 30.0,
        shutdown_grace_s: float = 15.0,
        on_run_changed: Callable[[str], Any] | None = None,
    ) -> None:
        if max_concurrency <= 0 or max_queue_size < 0:
            raise ValueError("scheduler concurrency must be positive and queue size non-negative")
        if run_timeout_s <= 0 or heartbeat_interval_s <= 0 or lease_ttl_s <= 0 or shutdown_grace_s < 0:
            raise ValueError("scheduler timeouts must be positive (grace may be zero)")
        if repository is None and store is None:
            raise ValueError("scheduler requires a repository or run store")
        self.repository = repository
        self.store = store
        self.worker = worker
        self.max_concurrency = int(max_concurrency)
        self.max_queue_size = int(max_queue_size)
        self.run_timeout_s = float(run_timeout_s)
        self.heartbeat_interval_s = float(heartbeat_interval_s)
        self.lease_ttl_s = float(lease_ttl_s)
        self.shutdown_grace_s = float(shutdown_grace_s)
        self.on_run_changed = on_run_changed
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        self._condition = threading.Condition(threading.RLock())
        self._queue: deque[str] = deque()
        self._active: dict[str, RunLease | None] = {}
        self._pending_ids: set[str] = set()
        self._started = False
        self._accepting = True
        self._dispatch_failed = False
        self._stopping = False
        self._dispatcher: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=self.max_concurrency, thread_name_prefix="agent-run")
        self._last_reconcile_monotonic = 0.0

    def start(self) -> None:
        with self._condition:
            if self._started:
                return
            if not self._accepting:
                return
            self._started = True
        if self.repository is not None:
            try:
                self.repository.reconcile_runs()
            except Exception:  # noqa: BLE001 - dispatcher retries reconciliation after startup
                logger.exception("initial run reconciliation failed")
            self._last_reconcile_monotonic = time.monotonic()
            snapshots = sorted(
                self._startup_snapshots(),
                key=lambda item: (str(item.get("created_at") or ""), str(item.get("run_id") or "")),
            )
            for snapshot in snapshots:
                request = snapshot.get("request") or {}
                eligible = snapshot.get("status") == "queued" or (
                    snapshot.get("status") == "waiting_user" and bool(request.get("resume_ready"))
                )
                if eligible:
                    with self._condition:
                        if not self._reserve_locked(str(snapshot["run_id"])):
                            break
        elif self.store is not None:
            snapshots = sorted(
                self._startup_snapshots(),
                key=lambda item: (str(item.get("created_at") or ""), str(item.get("run_id") or "")),
            )
            for snapshot in snapshots:
                if snapshot.get("status") == "queued":
                    with self._condition:
                        if not self._reserve_locked(str(snapshot["run_id"])):
                            break
        # These coordination threads are daemonized only as a process-safety
        # fallback; FastAPI lifespan owns their normal start/stop lifecycle.
        self._dispatcher = threading.Thread(target=self._dispatch_loop, name="agent-run-dispatch", daemon=True)
        self._heartbeat = threading.Thread(target=self._heartbeat_loop, name="agent-run-heartbeat", daemon=True)
        self._dispatcher.start()
        self._heartbeat.start()

    def _startup_snapshots(self) -> list[dict[str, Any]]:
        try:
            return self.repository.list_runs() if self.repository is not None else self.store.list()
        except Exception:
            self.record_dispatch_failure()
            logger.exception("initial durable queue scan failed; dispatcher will retry")
            return []

    def _reserve_locked(self, run_id: str) -> bool:
        if run_id in self._pending_ids:
            if run_id not in self._active and run_id not in self._queue:
                self._queue.append(run_id)
                self._condition.notify_all()
            return True
        if len(self._pending_ids) >= self.max_concurrency + self.max_queue_size:
            return False
        self._pending_ids.add(run_id)
        self._queue.append(run_id)
        self._condition.notify_all()
        return True

    def submit(self, run_id: str) -> SubmitResult:
        self.start()
        value = str(run_id)
        with self._condition:
            status = self._status(value)
            if not self._accepting:
                snapshot = self.repository.get_run(value) if self.repository is not None else self.store.load(value)
                if self._is_eligible(snapshot):
                    return SubmitResult(True, status, "scheduler_stopping")
                return SubmitResult(False, status, "not_eligible")
            if value in self._pending_ids:
                return SubmitResult(True, status)
            if not self._reserve_locked(value):
                # The persisted run is the durable queue. Keep the in-memory
                # queue bounded and let the dispatcher reserve it when space
                # becomes available instead of returning 503 for accepted work.
                return SubmitResult(True, status, "deferred_capacity")
            return SubmitResult(True, "queued")

    def _status(self, run_id: str) -> str:
        try:
            snapshot = self.repository.get_run(run_id) if self.repository is not None else self.store.load(run_id)
            return str(snapshot.get("status") or "queued")
        except (KeyError, ValueError):
            return "queued"

    def _dispatch_loop(self) -> None:
        while True:
            run_id: str | None = None
            with self._condition:
                while not self._stopping and (not self._queue or len(self._active) >= self.max_concurrency):
                    self._condition.wait(timeout=0.25)
                    if not self._queue and len(self._active) < self.max_concurrency:
                        break
                if self._stopping:
                    return
                if self._queue and len(self._active) < self.max_concurrency:
                    run_id = self._queue.popleft()
                    self._active[run_id] = None
                    self._condition.notify_all()
            if run_id is None:
                self._discover_durable_work()
                continue
            try:
                self._executor.submit(self._execute, run_id)
                with self._condition:
                    self._dispatch_failed = False
            except RuntimeError:
                self.record_dispatch_failure()
                with self._condition:
                    self._active.pop(run_id, None)
                    if not self._stopping and run_id not in self._queue:
                        self._queue.appendleft(run_id)
                    self._condition.notify_all()
                if not self._stopping:
                    logger.exception("failed to submit run %s to worker pool", run_id)
                    time.sleep(0.05)

    @staticmethod
    def _is_eligible(snapshot: dict[str, Any]) -> bool:
        request = snapshot.get("request") or {}
        return snapshot.get("status") == "queued" or (
            snapshot.get("status") == "waiting_user" and bool(request.get("resume_ready"))
        )

    def _discover_durable_work(self) -> None:
        now = time.monotonic()
        if self.repository is not None and now - self._last_reconcile_monotonic >= 2.0:
            try:
                self.repository.reconcile_runs()
            except Exception:  # noqa: BLE001 - keep dispatch alive and retry on the next pass
                logger.exception("periodic run reconciliation failed")
            self._last_reconcile_monotonic = now
        try:
            snapshots = self.repository.list_runs() if self.repository is not None else self.store.list()
        except Exception:  # noqa: BLE001 - a temporary storage error must not kill dispatch
            self.record_dispatch_failure()
            logger.exception("could not scan durable run queue")
            return
        with self._condition:
            self._dispatch_failed = False
        snapshots.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("run_id") or "")))
        with self._condition:
            for snapshot in snapshots:
                run_id = str(snapshot.get("run_id") or "")
                if run_id and self._is_eligible(snapshot) and run_id not in self._active:
                    self._reserve_locked(run_id)

    def _acquire(self, run_id: str) -> RunLease | None:
        if self.repository is not None:
            data = self.repository.acquire_run_lease(run_id, self.worker_id, lease_ttl_s=self.lease_ttl_s)
            if data is None:
                return None
            return RunLease(
                run_id=run_id,
                worker_id=self.worker_id,
                token=str(data["token"]),
                expires_at=str(data["expires_at"]),
                attempt=int(data["attempt"]),
                queue_wait_ms=(
                    max(0, int(data["queue_wait_ms"]))
                    if isinstance(data.get("queue_wait_ms"), (int, float)) else None
                ),
                lease_deadline_monotonic=time.monotonic() + self.lease_ttl_s,
            )
        snapshot = self.store.load(run_id)
        status = snapshot.get("status")
        queue_wait_ms = _elapsed_since_ms(snapshot.get("updated_at") or snapshot.get("created_at"))
        if status == "queued" and not self.store.claim_start(run_id):
            return None
        if status == "waiting_user" and not self.store.claim_waiting(run_id):
            return None
        if status not in {"queued", "waiting_user", "running"}:
            return None
        return RunLease(
            run_id, self.worker_id, None, None, int(snapshot.get("attempt") or 1),
            queue_wait_ms=queue_wait_ms,
        )

    def _execute(self, run_id: str) -> None:
        lease: RunLease | None = None
        try:
            lease = self._acquire(run_id)
            if lease is None:
                return
            lease.started_monotonic = time.monotonic()
            with self._condition:
                if run_id in self._active:
                    self._active[run_id] = lease
                    self._condition.notify_all()
            self.worker(lease)
        except Exception as exc:  # noqa: BLE001 - make worker failures visible and retryable
            if lease is not None:
                message = str(exc) or "Agent worker failed"
                self._finish_interrupted(lease, "worker_error", message)
        finally:
            try:
                snapshot = self.repository.get_run(run_id) if self.repository is not None else self.store.load(run_id)
            except Exception:  # noqa: BLE001 - periodic durable scan can recover if storage is temporarily unavailable
                logger.exception("could not inspect run %s after worker exit", run_id)
                snapshot = None
            with self._condition:
                self._active.pop(run_id, None)
                if lease is not None and snapshot is not None and self._is_eligible(snapshot) and not self._stopping:
                    self._reserve_locked(run_id)
                else:
                    self._pending_ids.discard(run_id)
                self._condition.notify_all()

    def _finish_interrupted(self, lease: RunLease, failure_code: str, message: str) -> None:
        try:
            if self.repository is not None:
                changed = self.repository.finish_run(
                    lease.run_id,
                    error=message,
                    lease_token=lease.token,
                    failure_code=failure_code,
                    retryable=True,
                )
            else:
                changed = self.store.finish(lease.run_id, error=message)
            if changed is not None and self.on_run_changed is not None:
                self.on_run_changed(lease.run_id)
        except (KeyError, RuntimeError, TypeError):
            pass

    def _heartbeat_loop(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                active = list(self._active.values())
            now = time.monotonic()
            for lease in active:
                if lease is None:
                    continue
                if now - lease.started_monotonic >= self.run_timeout_s:
                    if not lease.cancel_event.is_set():
                        lease.cancel_event.set()
                        self._finish_interrupted(lease, "execution_timeout", "Agent 运行超过时限，已停止并可重试。")
                    continue
                if self.repository is not None and lease.token:
                    try:
                        renewed = self.repository.heartbeat_run(
                            lease.run_id, lease.token, lease_ttl_s=self.lease_ttl_s
                        )
                    except Exception:  # noqa: BLE001 - do not treat an uncommitted renewal as success
                        logger.exception("heartbeat failed for run %s", lease.run_id)
                        if lease.lease_deadline_monotonic and now >= lease.lease_deadline_monotonic:
                            lease.cancel_event.set()
                        continue
                    if renewed:
                        lease.lease_deadline_monotonic = now + self.lease_ttl_s
                    else:
                        lease.cancel_event.set()
            time.sleep(self.heartbeat_interval_s)

    def record_dispatch_failure(self) -> None:
        with self._condition:
            self._dispatch_failed = True

    def stats(self) -> SchedulerStats:
        with self._condition:
            return SchedulerStats(
                active_count=len(self._active),
                queue_depth=len(self._queue),
                accepting=self._accepting,
                max_concurrency=self.max_concurrency,
                max_queue_size=self.max_queue_size,
                healthy=not self._dispatch_failed and (self._dispatcher is None or self._dispatcher.is_alive()),
            )

    def queue_position(self, run_id: str) -> int | None:
        value = str(run_id)
        with self._condition:
            try:
                return list(self._queue).index(value) + 1
            except ValueError:
                return None

    def cancel(self, run_id: str) -> bool:
        value = str(run_id)
        lease: RunLease | None = None
        with self._condition:
            if value in self._queue:
                self._queue = deque(item for item in self._queue if item != value)
                self._pending_ids.discard(value)
                self._condition.notify_all()
            lease = self._active.get(value)
            if lease is not None:
                lease.cancel_event.set()
        if self.repository is not None:
            result = self.repository.cancel_run(value, lease_token=lease.token if lease else None)
        else:
            result = self.store.cancel(value)
        return result is not None

    def shutdown(self, grace_seconds: float | None = None) -> None:
        self.start()
        grace = self.shutdown_grace_s if grace_seconds is None else max(0.0, float(grace_seconds))
        with self._condition:
            if self._stopping:
                return
            self._accepting = False
            self._condition.notify_all()
        deadline = time.monotonic() + grace
        with self._condition:
            while (self._active or self._queue) and time.monotonic() < deadline:
                self._condition.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
            self._stopping = True
            self._condition.notify_all()
            unfinished = [lease for lease in self._active.values() if lease is not None]
        for lease in unfinished:
            lease.cancel_event.set()
            self._finish_interrupted(lease, "shutdown_interrupted", "服务关闭时运行仍未完成，可显式重试。")
        if self._dispatcher is not None:
            self._dispatcher.join(timeout=1)
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=max(0.1, self.heartbeat_interval_s * 2))
        self._executor.shutdown(wait=False, cancel_futures=True)
