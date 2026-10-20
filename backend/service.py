"""可选 HTTP 服务：运行提交、状态查询和可回放 SSE。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import os
import time
from contextvars import ContextVar
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse

from backend.agent_runtime.loop import run_agent
from backend.agent_runtime.control_tools import validate_answers
from backend.agent_runtime.lifecycle import outcome_from_snapshot
from backend.agent_runtime.run_contracts import RunContractError
from backend.agent_runtime.run_store import RunStore, canonical_request_hash
from backend.agent_runtime.event_notifier import RunEventNotifier
from backend.agent_runtime.session_store import SessionStore
from backend.agent_runtime.sqlite_repository import SQLiteRuntimeRepository, SQLiteRunStore, SQLiteSessionStore, classify_sqlite_failure
from backend.agent_runtime.conversation_storage import ConversationStorage
from backend.conversation_api import create_conversation_router
from backend.analysis_api import create_analysis_router
from backend.agent_runtime.scheduler import RunLease, RunScheduler
from backend.agent_runtime.mcp import MCPManager
from backend.agent_runtime.state import CHECKPOINT_VERSION, CheckpointMigrationRequired
from backend.agent_runtime.context_budget import (
    DEFAULT_CONTEXT_BUDGET,
    ContextBudget,
    TokenEstimator,
    user_input_exceeds_budget,
)
from backend.agent_runtime.nodes import SYSTEM_PROMPT
from backend.agent_runtime.answer_contract import ANSWER_SCHEMA_VERSION
from backend.agent_runtime.runtime_projection import RUNTIME_PROTOCOL_VERSION
from backend.agent_runtime.tool_registry import stable_tool_schemas
from backend.api_projection import public_run_event, public_run_snapshot, public_session_message
from backend.auth import (
    OWNER_COOKIE_MAX_AGE_S,
    RequestPrincipal,
    configured_owner_cookie_secret,
    make_principal_resolver,
)
from backend.config import (
    OWNER_COOKIE_MAX_AGE_S as CONFIG_OWNER_COOKIE_MAX_AGE_S,
    OWNER_COOKIE_SECURE,
    SCHEDULER_HEARTBEAT_INTERVAL_S,
    SCHEDULER_LEASE_TTL_S,
    SCHEDULER_MAX_CONCURRENCY,
    SCHEDULER_MAX_QUEUE_SIZE,
    SCHEDULER_RUN_TIMEOUT_S,
    SCHEDULER_SHUTDOWN_GRACE_S,
    MCP_COOLDOWN_S,
    MCP_FAILURE_THRESHOLD,
    MCP_MAX_WORKERS,
    MCP_TIMEOUT_S,
    MIDRUN_INPUT_ENABLED,
    RUN_RECOVERY_ENABLED,
    CONTEXT_MAX_INPUT_TOKENS,
    CONTEXT_MAX_INPUT_CHARACTERS,
    CONTEXT_MAX_OUTPUT_TOKENS,
    CONTEXT_MAX_CONTEXT_TOKENS,
    MODEL_MAX_OUTPUT_TOKENS,
    MODEL_READ_TIMEOUT_S,
    RUN_BUDGET_LIMITS,
)
from backend.tools.data_contract import get_data_capabilities
from backend.data_loader import DatasetChangedDuringReadError


logger = logging.getLogger(__name__)
_request_diagnostic: ContextVar[dict[str, Any] | None] = ContextVar("request_diagnostic", default=None)


def _stage(value: str, **fields: Any) -> None:
    diagnostic = _request_diagnostic.get()
    if diagnostic is not None:
        diagnostic.update(stage=value, **fields)


def _failure_detail(exc: Exception, diagnostic: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    classified = classify_sqlite_failure(exc)
    code, transient = classified or ("internal_error", False)
    if isinstance(exc, DatasetChangedDuringReadError):
        code, transient = "data_snapshot_changed", True
    stage = diagnostic.get("stage", "request")
    if code == "internal_error" and stage == "preflight":
        code = "submission_preflight_failed"
    detail = {"code": code, "message": "服务暂时繁忙，请稍后使用同一请求重试。" if transient else "请求处理失败，请根据请求编号查看服务日志。",
              "retryable": transient, "request_id": diagnostic["request_id"], "stage": stage,
              "acceptance": "accepted" if stage == "accepted" else "not_accepted" if stage in {"preflight", "capabilities"} else "unknown"}
    if diagnostic.get("run_id"):
        detail["run_id"] = diagnostic["run_id"]
    return (503 if transient else 500), detail


def create_app(
    *,
    run_store: RunStore | None = None,
    session_store: SessionStore | None = None,
    runner: Callable[..., dict[str, Any]] = run_agent,
    owner_cookie_secret: str | None = None,
    owner_cookie_secure: bool | None = None,
    owner_cookie_max_age_s: int | None = None,
    public_access_token: str | None = None,
    admission_limits: dict[str, int] | None = None,
    context_budget: ContextBudget | None = None,
    token_estimator: TokenEstimator | None = None,
    runtime_repository: SQLiteRuntimeRepository | None = None,
    mcp_manager: MCPManager | None = None,
) -> FastAPI:
    if runtime_repository is None and (run_store is None or session_store is None):
        runtime_repository = SQLiteRuntimeRepository()
    runs = run_store or (SQLiteRunStore(runtime_repository) if runtime_repository is not None else RunStore())
    sessions = session_store or (SQLiteSessionStore(runtime_repository) if runtime_repository is not None else SessionStore())
    run_context_budget = context_budget or DEFAULT_CONTEXT_BUDGET
    run_token_estimator = token_estimator or TokenEstimator()
    expected_public_token = (public_access_token if public_access_token is not None
                             else os.getenv("APP_PUBLIC_ACCESS_TOKEN", "").strip())
    runtime_admission_limits = admission_limits if admission_limits is not None else {
        "global_pending": max(0, int(os.getenv("RUNTIME_MAX_PENDING_GLOBAL", "0") or 0)),
        "owner_pending": max(0, int(os.getenv("RUNTIME_MAX_PENDING_PER_OWNER", "0") or 0)),
        "daily_tokens": max(0, int(os.getenv("RUNTIME_DAILY_TOKEN_LIMIT", "0") or 0)),
    }
    sse_poll_interval = max(0.05, float(os.getenv("RUNTIME_SSE_POLL_INTERVAL_SECONDS", "2") or 2))

    def estimate_reservation(_text: str = "", *, remaining_model_calls: int | None = None) -> int:
        calls = RUN_BUDGET_LIMITS["model_calls"] if remaining_model_calls is None else max(0, int(remaining_model_calls))
        return calls * (run_context_budget.input_tokens + run_context_budget.reserved_output_tokens)

    def remaining_model_calls(snapshot: dict[str, Any]) -> int:
        checkpoint = snapshot.get("checkpoint") if isinstance(snapshot.get("checkpoint"), dict) else {}
        budget = checkpoint.get("run_budget") if isinstance(checkpoint.get("run_budget"), dict) else {}
        limits = budget.get("limits") if isinstance(budget.get("limits"), dict) else {}
        maximum = max(0, int(limits.get("model_calls", RUN_BUDGET_LIMITS["model_calls"])))
        return max(0, maximum - max(0, int(budget.get("model_calls") or 0)))
    production = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "")).strip().lower() in {"prod", "production"}
    secret = owner_cookie_secret if owner_cookie_secret is not None else os.getenv("OWNER_COOKIE_SECRET", "").strip()
    if production and not secret:
        raise RuntimeError("生产环境必须设置 OWNER_COOKIE_SECRET")
    if not secret:
        secret = configured_owner_cookie_secret()
    cookie_secure = owner_cookie_secure if owner_cookie_secure is not None else (OWNER_COOKIE_SECURE or production)
    if production and not cookie_secure:
        raise RuntimeError("生产环境必须启用 OWNER_COOKIE_SECURE")
    cookie_max_age = int(owner_cookie_max_age_s or CONFIG_OWNER_COOKIE_MAX_AGE_S or OWNER_COOKIE_MAX_AGE_S)
    if cookie_max_age <= 0:
        raise ValueError("OWNER_COOKIE_MAX_AGE_S 必须为正数")
    owner_dependency = make_principal_resolver(secret, secure=cookie_secure, max_age_s=cookie_max_age)
    runtime_mcp_manager = mcp_manager or MCPManager(
        timeout_s=MCP_TIMEOUT_S,
        failure_threshold=MCP_FAILURE_THRESHOLD,
        cooldown_s=MCP_COOLDOWN_S,
        max_workers=MCP_MAX_WORKERS,
    )
    event_notifier = RunEventNotifier()
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        logger.info(
            "Agent context limits active: input=%s output=%s model_context=%s model_output=%s "
            "estimator=utf8_json_byte_approximation read_timeout_seconds=%s",
            run_context_budget.input_tokens,
            run_context_budget.reserved_output_tokens,
            CONTEXT_MAX_CONTEXT_TOKENS,
            MODEL_MAX_OUTPUT_TOKENS,
            MODEL_READ_TIMEOUT_S,
        )
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()
            runtime_mcp_manager.close()

    app = FastAPI(title="二手车智能分析 Agent", version="1.0", lifespan=lifespan)
    app.state.runtime_repository = runtime_repository
    app.state.run_store = runs
    app.state.session_store = sessions
    app.state.mcp_manager = runtime_mcp_manager
    app.state.run_event_notifier = event_notifier
    app.state.sse_poll_interval_seconds = sse_poll_interval
    app.state.context_budget_config = run_context_budget.to_config()
    app.state.model_capabilities = {
        "max_context_tokens": CONTEXT_MAX_CONTEXT_TOKENS,
        "max_output_tokens": MODEL_MAX_OUTPUT_TOKENS,
        "max_input_tokens": CONTEXT_MAX_INPUT_TOKENS,
        "max_output_budget_tokens": CONTEXT_MAX_OUTPUT_TOKENS,
    }

    @app.exception_handler(HTTPException)
    async def expected_http_error(request: Request, exc: HTTPException):
        diagnostic = getattr(request.state, "diagnostic", {"request_id": uuid4().hex, "stage": "request"})
        raw = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        detail = {**raw, "code": raw.get("code", "request_rejected"),
                  "message": raw.get("message", "请求未能处理。"),
                  "retryable": bool(raw.get("retryable", False)),
                  "request_id": diagnostic["request_id"], "stage": diagnostic.get("stage", "request")}
        return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=exc.headers)

    @app.middleware("http")
    async def bind_owner_cookie(request: Request, call_next):
        diagnostic = {"request_id": uuid4().hex, "stage": "request"}
        request.state.diagnostic = diagnostic
        token = _request_diagnostic.set(diagnostic)
        started = time.monotonic()
        cookie_header = None
        try:
            if request.url.path.startswith("/v1/") and request.method != "OPTIONS":
                cookie_response = Response()
                request.state.owner_principal = owner_dependency(request, cookie_response)
                cookie_header = cookie_response.headers.get("set-cookie")
            response = await call_next(request)
        except Exception as exc:  # errors are handled inside the cookie/CORS boundary
            status, detail = _failure_detail(exc, diagnostic)
            logger.exception("HTTP request failed request_id=%s method=%s route=%s stage=%s run_id=%s elapsed_ms=%s exception=%s",
                             diagnostic["request_id"], request.method, request.url.path,
                             diagnostic["stage"], diagnostic.get("run_id"),
                             int((time.monotonic() - started) * 1000), type(exc).__name__)
            response = JSONResponse(status_code=status, content={"detail": detail},
                                    headers={"Retry-After": "5"} if status == 503 else None)
        logger.info("HTTP request completed request_id=%s method=%s route=%s stage=%s run_id=%s status=%s elapsed_ms=%s",
                    diagnostic["request_id"], request.method, request.url.path, diagnostic["stage"],
                    diagnostic.get("run_id"), response.status_code, int((time.monotonic() - started) * 1000))
        response.headers["X-Request-ID"] = diagnostic["request_id"]
        if cookie_header:
            response.headers.append("set-cookie", cookie_header)
        _request_diagnostic.reset(token)
        return response

    @app.middleware("http")
    async def guard_public_writes(request: Request, call_next):
        if expected_public_token and request.method.upper() != "OPTIONS" and request.url.path.startswith("/v1/"):
            supplied = request.headers.get("x-app-access-token", "")
            authorization = request.headers.get("authorization", "")
            if authorization.lower().startswith("bearer "):
                supplied = authorization[7:].strip()
            if not hmac.compare_digest(supplied.encode("utf-8"), expected_public_token.encode("utf-8")):
                return JSONResponse(status_code=401, content={"detail": {"code": "deployment_access_token_required"}})
        return await call_next(request)

    async def current_principal(request: Request) -> RequestPrincipal:
        principal = getattr(request.state, "owner_principal", None)
        if not isinstance(principal, RequestPrincipal):
            raise HTTPException(status_code=401, detail="缺少浏览器 owner 身份")
        return principal

    @app.get("/v1/reports/{report_id}")
    def get_report(report_id: str, principal: RequestPrincipal = Depends(current_principal)):
        if runtime_repository is None:
            raise HTTPException(status_code=501, detail={"code": "report_artifact_storage_unavailable"})
        try:
            return ConversationStorage(runtime_repository).report(report_id, principal.owner_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "report_not_found"}) from exc

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Last-Event-ID", "Authorization", "X-App-Access-Token"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/capabilities")
    async def data_capabilities(
        _principal: RequestPrincipal = Depends(current_principal),
    ) -> dict[str, Any]:
        _stage("capabilities")
        try:
            return await asyncio.to_thread(get_data_capabilities)
        except DatasetChangedDuringReadError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "data_capabilities_unavailable"},
            ) from exc

    @app.get("/health/ready")
    def health_ready() -> dict[str, Any]:
        try:
            scheduler.start()
            if runtime_repository is not None:
                connection = runtime_repository._connect()
                connection.close()
            stats = scheduler.stats()
            if not stats.accepting or not stats.healthy:
                raise RuntimeError("scheduler_stopping")
            return {"status": "ready", "scheduler": stats.__dict__}
        except Exception as exc:  # noqa: BLE001 - readiness must expose a stable HTTP failure
            raise HTTPException(status_code=503, detail={"code": "scheduler_not_ready", "status": "not_ready", "message": "调度或存储服务尚未就绪。"}) from exc

    @app.get("/health/runtime")
    def health_runtime() -> dict[str, Any]:
        stats = scheduler.stats()
        return {
            "status": "ok" if stats.accepting and stats.healthy else "degraded",
            "scheduler": stats.__dict__,
            "storage": "sqlite" if runtime_repository is not None else "json_compatibility",
            "runtime_protocol_version": RUNTIME_PROTOCOL_VERSION,
            "checkpoint_version": CHECKPOINT_VERSION,
            "answer_schema_version": ANSWER_SCHEMA_VERSION,
            "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16],
        }

    leased_repository = runs.repository if isinstance(runs, SQLiteRunStore) else None
    runtime_midrun_input_enabled = bool(MIDRUN_INPUT_ENABLED and leased_repository is not None)
    runtime_recovery_enabled = bool(RUN_RECOVERY_ENABLED and leased_repository is not None)

    def checkpoint_is_recoverable(snapshot: dict[str, Any]) -> bool:
        checkpoint = snapshot.get("checkpoint")
        if (not runtime_recovery_enabled or snapshot.get("status") != "error"
                or snapshot.get("failure_code") != "worker_lost" or not isinstance(checkpoint, dict)):
            return False
        state = checkpoint
        if state.get("pending_input"):
            return False
        batch = state.get("pending_tool_batch")
        if isinstance(batch, dict):
            from backend.agent_runtime.tool_registry import get_tool
            remaining = list(batch.get("calls") or [])[int(batch.get("next_index") or 0):]
            for call in remaining:
                name = str(((call.get("function") or {}).get("name") or ""))
                spec = get_tool(name)
                if not name or name.startswith("mcp__") or spec is None or (
                    spec is not None and not spec.read_only and spec.effect != "agent_control"
                ):
                    return False
        return True

    def worker(lease: RunLease) -> None:
        run_id = lease.run_id
        snapshot = runs.load(run_id)
        request_data = snapshot.get("request") or {}
        session_id = str(snapshot.get("session_id") or "")
        context_id = str(snapshot.get("context_id") or session_id)
        input_text = str(snapshot.get("input") or "")
        owner_id = snapshot.get("owner_id")
        source_report_id = str(request_data.get("source_report_id") or snapshot.get("source_report_id") or "").strip()
        confirmed_filters = request_data.get("confirmed_filters") or {}
        reuse_current_user = bool(request_data.get("reuse_current_user"))
        resume_checkpoint = snapshot.get("checkpoint") if request_data.get("resume_requested") else None
        input_answers = request_data.get("input_answers") or []
        steering_message = None
        steering_message_id = str(request_data.get("steering_client_message_id") or "")
        if resume_checkpoint is not None and steering_message_id and leased_repository is not None:
            steering_message = next((item for item in runs.repository.pending_run_messages(
                run_id, lease_token=lease.token) if item.get("client_message_id") == steering_message_id), None)

        def append_event(event: dict[str, Any]) -> Any:
            if leased_repository is not None:
                accepted = runs.append(run_id, event, lease_token=lease.token)
                if accepted is None:
                    lease.cancel_event.set()
                else:
                    event_notifier.notify(run_id)
                return accepted
            accepted = runs.append(run_id, event)
            if accepted is not None:
                event_notifier.notify(run_id)
            return accepted

        def finish_run(result: dict[str, Any] | None = None, error: str | None = None, *, failure_code: str | None = None, retryable: bool = False) -> Any:
            session_commit = result.pop("_session_commit", None) if isinstance(result, dict) else None
            if leased_repository is not None:
                if error is None and (result or {}).get("status") == "done":
                    session_commit = session_commit if isinstance(session_commit, dict) else {}
                    completed = leased_repository.complete_run(
                        run_id,
                        result=result,
                        error=error,
                        failure_code=failure_code,
                        retryable=retryable,
                        session_messages=session_commit.get("messages") or [],
                        session_run_id=str(session_commit.get("run_id") or ""),
                        lease_token=lease.token,
                    )
                    if completed is None:
                        lease.cancel_event.set()
                    else:
                        event_notifier.notify(run_id)
                    return completed
                finished = runs.finish(
                    run_id, result=result, error=error, lease_token=lease.token,
                    failure_code=failure_code, retryable=retryable,
                )
                if finished is not None:
                    event_notifier.notify(run_id)
                return finished
            if isinstance(session_commit, dict) and session_commit.get("messages"):
                sessions.append_messages(
                    context_id,
                    session_commit.get("messages") or [],
                    run_id=str(session_commit.get("run_id") or run_id),
                    owner_id=owner_id,
                )
            finished = runs.finish(
                run_id,
                result=result,
                error=error,
                failure_code=failure_code,
                retryable=retryable,
            )
            if finished is not None:
                event_notifier.notify(run_id)
            return finished

        try:
            append_event({
                "event_type": "started",
                "session_id": session_id,
                "queue_wait_ms": lease.queue_wait_ms,
                "attempt": lease.attempt,
            })
            deferred_events: list[dict[str, Any]] = []

            def sink(event: dict[str, Any]) -> None:
                # The Agent can emit input_required slightly before it returns
                # its waiting checkpoint. Keep that event private until the
                # RunStore has atomically moved to waiting_user; otherwise a
                # fast client can submit against a still-running run and the
                # same question can be rendered twice.
                if event.get("event_type") == "input_required":
                    deferred_events.append(event)
                    return
                append_event(event)

            runner_args: dict[str, Any] = {
                "user_text": input_text,
                "session_id": session_id,
                "context_id": context_id,
                "run_id": run_id,
                "session_store": sessions,
                "event_sink": sink,
                "confirmed_filters": confirmed_filters or {},
                "owner_id": owner_id,
                "context_budget": context_budget or run_context_budget,
                "token_estimator": token_estimator or run_token_estimator,
            }
            if source_report_id:
                runner_args["source_report_id"] = source_report_id
            runner_signature = None
            accepts_kwargs = False
            try:
                runner_signature = inspect.signature(runner)
                accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in runner_signature.parameters.values())
                if runner is run_agent or accepts_kwargs or "cancellation" in runner_signature.parameters:
                    runner_args["cancellation"] = lease.cancel_event
                if runner is run_agent or accepts_kwargs or "lease_token" in runner_signature.parameters:
                    runner_args["lease_token"] = lease.token
                if leased_repository is not None and (
                    runner is run_agent or accepts_kwargs or "execution_attempt" in runner_signature.parameters
                ):
                    runner_args["execution_attempt"] = lease.attempt
                if leased_repository is not None and (
                    runner is run_agent or accepts_kwargs or "defer_session_commit" in runner_signature.parameters
                ):
                    runner_args["defer_session_commit"] = True
                if mcp_manager is not None and (runner is run_agent or accepts_kwargs or "mcp_manager" in runner_signature.parameters):
                    runner_args["mcp_manager"] = runtime_mcp_manager
            except (TypeError, ValueError):
                pass
            if reuse_current_user:
                runner_args["reuse_current_user"] = True
            if resume_checkpoint is not None:
                runner_args["resume_checkpoint"] = resume_checkpoint
                runner_args["input_answers"] = ((input_answers or []) if resume_checkpoint.get("pending_input") and not steering_message else None)
                if steering_message is not None:
                    runner_args["steering_message"] = steering_message
            if leased_repository is not None and (runner is run_agent or accepts_kwargs or (runner_signature and "checkpoint_sink" in runner_signature.parameters)):
                def persist_checkpoint(checkpoint: dict[str, Any]) -> None:
                    if not runs.repository.save_run_checkpoint(run_id, checkpoint, lease_token=lease.token):
                        lease.cancel_event.set()
                runner_args["checkpoint_sink"] = persist_checkpoint
            if runtime_midrun_input_enabled and (runner is run_agent or accepts_kwargs or
                    (runner_signature and "message_source" in runner_signature.parameters)):
                runner_args["message_source"] = lambda: runs.repository.pending_run_messages(run_id, lease_token=lease.token)
                runner_args["message_ack"] = lambda messages, checkpoint: runs.repository.apply_run_messages(
                    run_id, messages, checkpoint, lease_token=lease.token
                )
            result = runner(
                **runner_args,
            )
            if isinstance(result, dict):
                capabilities = {"midrun_input": runtime_midrun_input_enabled,
                                "recover": runtime_recovery_enabled}
                result["capabilities"] = capabilities
                runtime_projection = result.get("runtime")
                if isinstance(runtime_projection, dict):
                    runtime_projection["capabilities"] = capabilities
            if result.get("status") == "waiting_user":
                session_commit = result.pop("_session_commit", None)
                state = result.get("state") or {}
                pending_input = state.get("pending_input") or result.get("pending_input") or {}
                waiting_event = deferred_events[-1] if deferred_events else {
                    "event_type": "input_required",
                    "pending_input": pending_input,
                }
                waiting_event = {
                    **waiting_event,
                    "pending_input": pending_input,
                }
                pause_kwargs = {
                    "checkpoint": result.get("checkpoint") or state,
                    "pending_input": pending_input,
                    "waiting_event": waiting_event,
                    "runtime": {
                        "status": "waiting_user",
                        "plan": state.get("plan") or [],
                        "activities": state.get("run_logs") or [],
                        "pending_input": pending_input,
                        "plan_revision": int((state.get("flags") or {}).get("plan_revision") or 0),
                        "input_revision": int((state.get("flags") or {}).get("input_revision") or 0),
                        "capabilities": {"midrun_input": runtime_midrun_input_enabled, "recover": runtime_recovery_enabled},
                    },
                    "result": result,
                }
                if leased_repository is not None:
                    pause_kwargs["lease_token"] = lease.token
                    if isinstance(session_commit, dict):
                        pause_kwargs["session_messages"] = session_commit.get("messages") or []
                        pause_kwargs["session_run_id"] = str(session_commit.get("run_id") or "")
                paused = runs.pause(run_id, **pause_kwargs)
                if paused is None:
                    return
                event_notifier.notify(run_id)
            else:
                if result.get("status") == "error":
                    finish_run(
                        result=result,
                        error=str(result.get("summary") or "Agent 运行失败。"),
                        failure_code=str(result.get("failure_code") or "agent_error"),
                        retryable=bool(result.get("retryable")),
                    )
                else:
                    finish_run(result=result)
        except CheckpointMigrationRequired as exc:
            finish_run(error=str(exc), failure_code=exc.failure_code, retryable=False)
        except Exception as exc:  # noqa: BLE001 - worker must close the run visibly
            finish_run(error=str(exc), failure_code="worker_error", retryable=True)

    scheduler = RunScheduler(
        repository=leased_repository,
        store=None if leased_repository is not None else runs,
        worker=worker,
        max_concurrency=SCHEDULER_MAX_CONCURRENCY,
        max_queue_size=SCHEDULER_MAX_QUEUE_SIZE,
        run_timeout_s=SCHEDULER_RUN_TIMEOUT_S,
        heartbeat_interval_s=SCHEDULER_HEARTBEAT_INTERVAL_S,
        lease_ttl_s=SCHEDULER_LEASE_TTL_S,
        shutdown_grace_s=SCHEDULER_SHUTDOWN_GRACE_S,
        on_run_changed=event_notifier.notify,
    )
    app.state.scheduler = scheduler

    def wake_accepted(run_id: str) -> None:
        _stage("accepted", run_id=run_id)
        try:
            submitted = scheduler.submit(run_id)
            if not submitted.accepted:
                logger.error("durable run dispatch deferred request_id=%s run_id=%s reason=%s",
                             (_request_diagnostic.get() or {}).get("request_id"), run_id, submitted.reason)
        except Exception:
            scheduler.record_dispatch_failure()
            logger.exception("durable run wake failed request_id=%s run_id=%s; dispatcher will rediscover",
                             (_request_diagnostic.get() or {}).get("request_id"), run_id)
        try:
            event_notifier.notify(run_id)
        except Exception:
            logger.exception("durable run notification failed run_id=%s", run_id)

    app.include_router(create_analysis_router(current_principal))
    app.include_router(create_conversation_router(
        ConversationStorage(runtime_repository) if runtime_repository is not None else None,
        current_principal,
        wake_accepted=wake_accepted,
        midrun_input_enabled=runtime_midrun_input_enabled,
        admission_limits=runtime_admission_limits,
        estimate_reservation=estimate_reservation,
    ))

    @app.post("/v1/runs", status_code=202)
    def create_run(
        payload: dict[str, Any],
        principal: RequestPrincipal = Depends(current_principal),
    ) -> dict[str, Any]:
        input_text = str(payload.get("input") or payload.get("message") or "").strip()
        if not input_text:
            raise HTTPException(status_code=400, detail="input 不能为空")
        if len(input_text) > CONTEXT_MAX_INPUT_CHARACTERS:
            raise HTTPException(
                status_code=413,
                detail={"code": "input_too_long", "max_characters": CONTEXT_MAX_INPUT_CHARACTERS},
            )
        raw_filters = payload.get("filters") or {}
        if not isinstance(raw_filters, dict):
            raise HTTPException(status_code=400, detail="filters 必须是对象")
        run_id = str(payload.get("run_id") or f"run-{uuid4().hex}")
        client_request_id = str(payload.get("client_request_id") or "").strip() or None
        # A caller-provided run_id is the compatibility idempotency key for
        # older clients. Keep implicit history/context ids stable across a
        # repeated POST with that same run_id.
        request_scope = client_request_id or run_id
        session_id = str(payload.get("session_id") or f"session-{request_scope[-96:]}")
        context_id = str(payload.get("context_id") or session_id)
        budgeted_input = input_text
        if raw_filters:
            budgeted_input = (
                f"{input_text}\n\n"
                "用户已确认的动态筛选条件（只能作为当前问题上下文，必须以工具实际支持字段为准）：\n"
                f"{json.dumps(raw_filters, ensure_ascii=False, sort_keys=True)}"
            )
        _stage("preflight")
        if user_input_exceeds_budget(
            budgeted_input,
            system_prompt=SYSTEM_PROMPT,
            budget=run_context_budget,
            estimator=run_token_estimator,
            tools=stable_tool_schemas(),
        ):
            raise HTTPException(status_code=413, detail="context_input_too_large")
        _stage("persistence")
        try:
            existing = runs.create(
                run_id,
                session_id,
                input_text,
                context_id=context_id,
                owner_id=principal.owner_id,
                request_data={"confirmed_filters": raw_filters},
                client_request_id=client_request_id,
                admission_limits=runtime_admission_limits if leased_repository is not None else None,
                reserve_tokens=estimate_reservation(budgeted_input),
            )
        except ValueError as exc:
            if str(exc).startswith("admission_"):
                raise HTTPException(status_code=429, detail={"code": str(exc), "acceptance": "not_accepted"}) from exc
            status_code = 409 if str(exc) in {"run_id_conflict", "client_request_conflict"} else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="运行不存在") from exc
        persisted_run_id = str(existing["run_id"])
        if existing.get("status") != "queued":
            return {
                "run_id": persisted_run_id,
                "session_id": str(existing.get("session_id") or session_id),
                "context_id": str(existing.get("context_id") or context_id),
                "status": existing["status"],
                "capabilities": {"midrun_input": runtime_midrun_input_enabled, "recover": runtime_recovery_enabled},
                "input_revision": int((existing.get("runtime") or {}).get("input_revision") or 0),
            }
        wake_accepted(persisted_run_id)
        persisted = existing
        return {
            "run_id": persisted_run_id,
            "session_id": str(persisted.get("session_id") or session_id),
            "context_id": str(persisted.get("context_id") or context_id),
            "status": persisted["status"],
            "capabilities": {"midrun_input": runtime_midrun_input_enabled, "recover": runtime_recovery_enabled},
            "input_revision": int((persisted.get("runtime") or {}).get("input_revision") or 0),
        }

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, principal: RequestPrincipal = Depends(current_principal)) -> dict[str, Any]:
        try:
            snapshot = await asyncio.to_thread(runs.load_owned, run_id, principal.owner_id)
            snapshot["capabilities"] = {"midrun_input": runtime_midrun_input_enabled,
                                        "recover": runtime_recovery_enabled}
            snapshot["input_revision"] = int((snapshot.get("runtime") or {}).get("input_revision") or 0)
            snapshot["recoverable"] = checkpoint_is_recoverable(snapshot)
            if snapshot["recoverable"]:
                snapshot["recovery_reason"] = "已保存安全边界检查点，可从最近的已完成工具之后恢复。"
            elif snapshot.get("failure_code") == "worker_lost":
                snapshot["recovery_reason"] = "未找到可安全恢复的检查点。"
            position = scheduler.queue_position(run_id)
            if position is not None:
                snapshot = {**snapshot, "queue_position": position}
            outcome_from_snapshot(snapshot)
            return public_run_snapshot(snapshot)
        except (KeyError, ValueError) as exc:
            if isinstance(exc, RunContractError):
                raise HTTPException(
                    status_code=500,
                    detail={"code": "protocol_invariant", "message": str(exc)},
                ) from exc
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/history")
    def list_history(
        session_id: str,
        principal: RequestPrincipal = Depends(current_principal),
    ) -> list[dict[str, Any]]:
        if not str(session_id or "").strip():
            raise HTTPException(status_code=400, detail="session_id 不能为空")
        records: list[dict[str, Any]] = []
        for snapshot in runs.list_owned(principal.owner_id, session_id):
            result = snapshot.get("result") or {}
            report = result.get("report") if isinstance(result, dict) else None
            report = report if isinstance(report, dict) else {}
            reliability = report.get("reliability") or {}
            status = str(snapshot.get("status") or "queued")
            records.append(
                {
                    "id": str(snapshot.get("run_id")),
                    "question": str(snapshot.get("input") or ""),
                    "intent": str(report.get("intent") or "unknown"),
                    "scenarioLabel": str(report.get("scenarioLabel") or "Agent 分析"),
                    "createdAt": str(
                        report.get("generatedAt")
                        or snapshot.get("updated_at")
                        or snapshot.get("created_at")
                        or ""
                    ),
                    "summary": str(
                        report.get("summaryText")
                        or result.get("summary")
                        or snapshot.get("error")
                        or "运行尚未完成。"
                    ),
                    "confidence": str(reliability.get("confidence") or "none"),
                    "reportId": str(snapshot.get("run_id")),
                    "status": status,
                    "retryable": bool(snapshot.get("retryable")),
                    "failureCode": snapshot.get("failure_code"),
                }
            )
        return records

    @app.get("/v1/runs/{run_id}/events")
    async def run_events(
        request: Request,
        run_id: str,
        after_seq: int = 0,
        principal: RequestPrincipal = Depends(current_principal),
    ) -> StreamingResponse:
        try:
            await asyncio.to_thread(runs.load_owned, run_id, principal.owner_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        header_cursor = request.headers.get("last-event-id")
        if header_cursor:
            try:
                after_seq = max(after_seq, int(header_cursor))
            except ValueError:
                pass

        async def stream():
            cursor = max(0, after_seq)
            idle_rounds = 0
            wake_event = event_notifier.subscribe(run_id)
            try:
                while idle_rounds < 600:
                    # Clear before the durable read: a concurrent write is then
                    # either visible in that read or leaves the event set.
                    wake_event.clear()
                    emitted = False
                    batch = await asyncio.to_thread(runs.read_after, run_id, cursor, 200)
                    for event in batch:
                        cursor = int(event["seq"])
                        projected = public_run_event(event)
                        event_name = str(projected.get("event_type") or "message")
                        yield f"id: {cursor}\nevent: {event_name}\ndata: {json.dumps(projected, ensure_ascii=False)}\n\n"
                        emitted = True
                    if len(batch) >= 200:
                        continue
                    status, last_seq = await asyncio.to_thread(runs.status_cursor, run_id)
                    if status in {"done", "error", "cancelled", "waiting_user"} and last_seq <= cursor:
                        break
                    if await request.is_disconnected():
                        break
                    idle_rounds = 0 if emitted else idle_rounds + 1
                    if not emitted:
                        yield ": keepalive\n\n"
                    try:
                        await asyncio.wait_for(wake_event.wait(), timeout=sse_poll_interval)
                    except asyncio.TimeoutError:
                        pass
            finally:
                event_notifier.unsubscribe(run_id, wake_event)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Run-Status": "replayable",
            },
        )

    @app.post("/v1/sessions/{session_id}/rollback")
    def rollback_session(
        session_id: str,
        payload: dict[str, Any],
        principal: RequestPrincipal = Depends(current_principal),
    ) -> dict[str, Any]:
        _stage("persistence")
        try:
            snapshot = sessions.rollback(
                session_id,
                str(payload.get("message_id") or ""),
                expected_revision=payload.get("expected_revision"),
                owner_id=principal.owner_id,
            )
            active = sessions.active_messages(session_id, owner_id=principal.owner_id)
            return {
                "session_id": session_id,
                "revision": snapshot.revision,
                "active_head_id": snapshot.active_head_id,
                "hidden_message_count": len(snapshot.messages) - len(active),
                "messages": [
                    projected
                    for message in active
                    if (projected := public_session_message(message))
                ],
            }
        except ValueError as exc:
            status = 409 if "revision" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="会话不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v1/runs/{run_id}/retry", status_code=202)
    def retry_run(run_id: str, payload: dict[str, Any] | None = None,
                        principal: RequestPrincipal = Depends(current_principal)) -> dict[str, Any]:
        try:
            previous = runs.load_owned(run_id, principal.owner_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if previous.get("status") != "error":
            raise HTTPException(status_code=409, detail="只有失败运行才能重试")
        if not bool(previous.get("retryable")):
            raise HTTPException(status_code=409, detail={"code": "run_not_retryable"})
        new_run_id = f"run-{uuid4().hex}"
        previous_request = previous.get("request") or {}
        previous_result = previous.get("result") or {}
        confirmed_filters = previous_request.get("confirmed_filters")
        if not isinstance(confirmed_filters, dict):
            confirmed_filters = previous_result.get("confirmed_filters", {}) if isinstance(previous_result, dict) else {}
        client_request_id = str((payload or {}).get("client_request_id") or "").strip() or None
        _stage("persistence")
        try:
            created = runs.create(
                new_run_id,
                previous["session_id"],
                previous["input"],
                context_id=str(previous.get("context_id") or previous["session_id"]),
                owner_id=principal.owner_id,
                request_data={"confirmed_filters": confirmed_filters, "reuse_current_user": True,
                              "retry_of": run_id},
                client_request_id=client_request_id,
                idempotency_operation="retry",
                admission_limits=runtime_admission_limits if leased_repository is not None else None,
                reserve_tokens=estimate_reservation(str(previous.get("input") or "")),
            )
        except ValueError as exc:
            if str(exc).startswith("admission_"):
                raise HTTPException(status_code=429, detail={"code": str(exc), "acceptance": "not_accepted"}) from exc
            if str(exc) == "client_request_conflict":
                raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if created.get("status") == "queued":
            wake_accepted(str(created["run_id"]))
        persisted = created
        return {
            "run_id": created["run_id"],
            "retry_of": run_id,
            "session_id": previous["session_id"],
            "context_id": str(previous.get("context_id") or previous["session_id"]),
            "status": persisted["status"],
        }

    @app.post("/v1/runs/{run_id}/cancel")
    def cancel_run(run_id: str, principal: RequestPrincipal = Depends(current_principal)) -> dict[str, Any]:
        try:
            runs.load_owned(run_id, principal.owner_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="运行不存在") from exc
        _stage("persistence")
        if not scheduler.cancel(run_id):
            raise HTTPException(status_code=409, detail="运行无法取消")
        _stage("accepted", run_id=run_id)
        try:
            event_notifier.notify(run_id)
        except Exception:
            logger.exception("durable cancellation notification failed run_id=%s", run_id)
        snapshot = runs.load_owned(run_id, principal.owner_id)
        position = scheduler.queue_position(run_id)
        if position is not None:
            snapshot = {**snapshot, "queue_position": position}
        try:
            outcome_from_snapshot(snapshot)
        except RunContractError as exc:
            raise HTTPException(
                status_code=500,
                detail={"code": "protocol_invariant", "message": str(exc)},
            ) from exc
        return public_run_snapshot(snapshot)

    @app.post("/v1/runs/{run_id}/recover", status_code=202)
    def recover_run(run_id: str, payload: dict[str, Any] | None = None,
                          principal: RequestPrincipal = Depends(current_principal)) -> dict[str, Any]:
        if not runtime_recovery_enabled:
            raise HTTPException(status_code=501, detail={"code": "recovery_unsupported", "recoverable": False})
        try:
            snapshot = runs.load_owned(run_id, principal.owner_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="运行不存在") from exc
        client_request_id = str((payload or {}).get("client_request_id") or "").strip() or None
        prior = leased_repository.peek_client_request(principal.owner_id, "recover", client_request_id) if client_request_id else None
        expected_version = payload.get("expected_version") if isinstance(payload, dict) else None
        if expected_version is None and prior is not None:
            expected_version = prior["response"].get("expected_version")
        if expected_version is None:
            expected_version = snapshot.get("version")
        try:
            expected_version = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="expected_version 必须是整数") from exc
        request_hash = canonical_request_hash({
            "operation": "recover", "run_id": run_id, "expected_version": expected_version,
        })
        if prior is not None:
            try:
                accepted_prior = leased_repository.get_client_request(
                    principal.owner_id, "recover", client_request_id, request_hash,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
            if accepted_prior is not None:
                accepted_snapshot = runs.load_owned(accepted_prior["run_id"], principal.owner_id)
                return {"run_id": accepted_prior["run_id"], "session_id": accepted_snapshot.get("session_id"),
                        "context_id": accepted_snapshot.get("context_id"), "status": "queued",
                        "recoverable": True, "recovery_reason": None,
                        "input_revision": int((accepted_snapshot.get("runtime") or {}).get("input_revision") or 0),
                        "capabilities": {"midrun_input": runtime_midrun_input_enabled,
                                         "recover": runtime_recovery_enabled}}
        if not checkpoint_is_recoverable(snapshot):
            raise HTTPException(status_code=409, detail={"code": "checkpoint_not_recoverable", "recoverable": False})
        _stage("persistence")
        try:
            recovered = leased_repository.recover_run(
                run_id, owner_id=principal.owner_id, expected_version=expected_version,
                client_request_id=client_request_id, request_hash=request_hash,
                admission_limits=runtime_admission_limits,
                reserve_tokens=estimate_reservation(str(snapshot.get("input") or ""),
                                                    remaining_model_calls=remaining_model_calls(snapshot)),
            )
        except ValueError as exc:
            if str(exc).startswith("admission_"):
                raise HTTPException(status_code=429, detail={"code": str(exc), "acceptance": "not_accepted"}) from exc
            if str(exc) == "client_request_conflict":
                raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if recovered is None:
            raise HTTPException(status_code=409, detail={"code": "recovery_conflict", "recoverable": False})
        if recovered.get("status") == "queued":
            wake_accepted(run_id)
        return {"run_id": run_id, "session_id": recovered.get("session_id"),
                "context_id": recovered.get("context_id"), "status": "queued",
                "recoverable": True, "recovery_reason": None,
                "input_revision": int((recovered.get("runtime") or {}).get("input_revision") or 0),
                "capabilities": {"midrun_input": runtime_midrun_input_enabled,
                                 "recover": runtime_recovery_enabled}}

    @app.post("/v1/runs/{run_id}/messages", status_code=202)
    def submit_run_message(run_id: str, payload: dict[str, Any],
                                 principal: RequestPrincipal = Depends(current_principal)) -> dict[str, Any]:
        if not runtime_midrun_input_enabled:
            raise HTTPException(status_code=501, detail={"code": "midrun_input_unsupported", "capability": False})
        client_message_id = str(payload.get("client_message_id") or "").strip()
        content = str(payload.get("content") or "").strip()
        if not client_message_id or not content:
            raise HTTPException(status_code=400, detail="client_message_id 和 content 必填")
        snapshot = runs.load_owned(run_id, principal.owner_id)
        _stage("persistence")
        try:
            expected_revision = int(payload.get("expected_revision"))
            accepted = runs.repository.accept_run_message(
                run_id, owner_id=principal.owner_id, client_message_id=client_message_id,
                content=content, expected_revision=expected_revision,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="expected_revision 必须是整数") from exc
        if accepted is None:
            raise HTTPException(status_code=409, detail={"code": "input_revision_conflict_or_run_not_running"})
        wake_accepted(run_id)
        return {"run_id": run_id, "session_id": snapshot.get("session_id"),
                "context_id": snapshot.get("context_id"), "status": "accepted",
                "client_message_id": accepted["client_message_id"], "input_revision": accepted["revision"],
                "revision": accepted["revision"],
                "capabilities": {"midrun_input": runtime_midrun_input_enabled,
                                 "recover": runtime_recovery_enabled}}

    @app.post("/v1/runs/{run_id}/input", status_code=202)
    def submit_run_input(
        run_id: str,
        payload: dict[str, Any],
        principal: RequestPrincipal = Depends(current_principal),
    ) -> dict[str, Any]:
        try:
            snapshot = runs.load_owned(run_id, principal.owner_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        answers = payload.get("answers")
        if not isinstance(answers, list):
            raise HTTPException(status_code=400, detail="answers 必须是数组")
        client_request_id = str(payload.get("client_request_id") or "").strip() or None
        command_hash = canonical_request_hash({
            "operation": "input",
            "run_id": run_id,
            "answers": answers,
            "tool_call_id": payload.get("tool_call_id"),
            "resume_revision": payload.get("resume_revision"),
        })
        if client_request_id and leased_repository is not None:
            try:
                prior = leased_repository.get_client_request(
                    principal.owner_id, "input", client_request_id, command_hash,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
            if prior is not None:
                response = prior["response"]
                current = runs.load_owned(prior["run_id"], principal.owner_id)
                return {
                    "run_id": prior["run_id"],
                    "status": "accepted",
                    "accepted_tool_call_id": response.get("accepted_tool_call_id", ""),
                    "resume_revision": response.get("resume_revision", 0),
                    "input_revision": int((current.get("runtime") or {}).get("input_revision") or 0),
                    "capabilities": {"midrun_input": runtime_midrun_input_enabled,
                                     "recover": runtime_recovery_enabled},
                }
        if snapshot.get("status") != "waiting_user":
            raise HTTPException(status_code=409, detail="当前运行不在等待用户输入状态")
        checkpoint = snapshot.get("checkpoint") or {}
        pending_input = snapshot.get("pending_input") or checkpoint.get("pending_input") or {}
        try:
            normalized_answers = validate_answers(
                list(pending_input.get("questions") or []),
                answers,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        requested_tool_call_id = payload.get("tool_call_id")
        requested_revision = payload.get("resume_revision")
        try:
            requested_revision = int(requested_revision) if requested_revision is not None else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="resume_revision 必须是整数") from exc
        request_data = snapshot.get("request") or {}
        previous_result = snapshot.get("result") or {}
        confirmed_filters = request_data.get("confirmed_filters")
        if not isinstance(confirmed_filters, dict):
            confirmed_filters = previous_result.get("confirmed_filters", {}) if isinstance(previous_result, dict) else {}
        request_data = {
            **request_data,
            "confirmed_filters": confirmed_filters,
            "resume_requested": True,
            "input_answers": normalized_answers,
        }
        expected_tool_call_id = str(requested_tool_call_id) if requested_tool_call_id is not None else None
        _stage("persistence")
        try:
            if leased_repository is not None:
                accepted_resume = runs.prepare_resume(
                    run_id,
                    request_data,
                    expected_tool_call_id=expected_tool_call_id,
                    expected_revision=requested_revision,
                    client_request_id=client_request_id,
                    request_hash=command_hash,
                )
            else:
                accepted_resume = runs.claim_waiting(
                    run_id,
                    expected_tool_call_id=expected_tool_call_id,
                    expected_revision=requested_revision,
                )
                if accepted_resume:
                    runs.update_request(run_id, request_data)
        except ValueError as exc:
            if str(exc) == "client_request_conflict":
                raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not accepted_resume:
            raise HTTPException(status_code=409, detail="该等待请求已被其他输入占用")
        wake_accepted(run_id)
        accepted = {**snapshot, "request": request_data, "resume_revision": int(snapshot.get("resume_revision") or 0) + 1}
        return {
            "run_id": run_id,
            "status": "accepted",
            # request_data can contain the previous question's accepted fields.
            # Return this command's identity, independent of worker timing.
            "accepted_tool_call_id": str(pending_input.get("tool_call_id") or ""),
            "resume_revision": int(accepted.get("resume_revision") or 0),
            "input_revision": int((accepted.get("runtime") or {}).get("input_revision") or 0),
            "capabilities": {"midrun_input": runtime_midrun_input_enabled,
                             "recover": runtime_recovery_enabled},
        }

    @app.get("/v1/sessions/{session_id}/messages")
    def get_session_messages(
        session_id: str,
        principal: RequestPrincipal = Depends(current_principal),
    ) -> dict[str, Any]:
        try:
            snapshot = sessions.load_owned(session_id, principal.owner_id)
            return {
                "session_id": snapshot.session_id,
                "revision": snapshot.revision,
                "messages": [
                    projected
                    for message in sessions.active_messages(session_id, owner_id=principal.owner_id)
                    if (projected := public_session_message(message))
                ],
            }
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="会话不存在") from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


app = create_app()
