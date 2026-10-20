"""Conversation read and creation API routes."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.agent_runtime.conversation_storage import ConversationStorage
from backend.auth import RequestPrincipal


def create_conversation_router(storage: ConversationStorage | None,
                               current_principal: Callable[..., Any],
                               wake_accepted: Callable[[str], None] | None = None,
                               midrun_input_enabled: bool = True,
                               admission_limits: dict[str, int] | None = None,
                               estimate_reservation: Callable[[str], int] | None = None) -> APIRouter:
    router = APIRouter(prefix="/v1/conversations", tags=["conversations"])

    def require_storage() -> ConversationStorage:
        if storage is None:
            raise HTTPException(status_code=501, detail={"code": "conversation_storage_unavailable"})
        return storage

    @router.post("", status_code=201)
    def create_conversation(payload: dict[str, Any], principal: RequestPrincipal = Depends(current_principal)):
        command_id = payload.get("client_request_id")
        try:
            return require_storage().create(principal.owner_id, str(command_id or ""))
        except ValueError as exc:
            status = 409 if str(exc) == "command_conflict" else 400
            raise HTTPException(status_code=status, detail={"code": str(exc)}) from exc

    @router.get("")
    def list_conversations(cursor: str | None = None, limit: int = Query(30, ge=1, le=100),
                           principal: RequestPrincipal = Depends(current_principal)):
        try:
            return require_storage().list(principal.owner_id, cursor=cursor, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": str(exc)}) from exc

    @router.get("/{conversation_id}/entries")
    def get_entries(conversation_id: str, before_seq: int | None = Query(None, ge=1),
                    limit: int = Query(50, ge=1, le=100),
                    principal: RequestPrincipal = Depends(current_principal)):
        try:
            return require_storage().entries(conversation_id, principal.owner_id, before_seq=before_seq, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "conversation_not_found"}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": str(exc)}) from exc

    @router.post("/{conversation_id}/messages", status_code=202)
    def send_message(conversation_id: str, payload: dict[str, Any],
                     principal: RequestPrincipal = Depends(current_principal)):
        try:
            command_id = str(payload.get("client_request_id") or payload.get("client_message_id") or "").strip()
            content = str(payload.get("content") or "").strip()
            if len(content) > 20_000:
                raise HTTPException(status_code=413, detail={"code": "input_too_long", "max_characters": 20000})
            accepted = require_storage().send_message(
                conversation_id, principal.owner_id, command_id, content,
                str(payload.get("source_report_id") or "").strip() or None,
                allow_steering=midrun_input_enabled,
                admission_limits=admission_limits,
                reserve_tokens=estimate_reservation(content) if estimate_reservation else 0,
            )
            if wake_accepted:
                wake_accepted(accepted["run_id"])
            return accepted
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "conversation_not_found"}) from exc
        except ValueError as exc:
            code = str(exc)
            status = 429 if code.startswith("admission_") else 501 if code == "midrun_input_unsupported" else 404 if code == "source_report_not_found" else 409 if code in {"command_conflict", "conversation_run_conflict", "run_finishing"} else 400
            raise HTTPException(status_code=status, detail={"code": code}) from exc

    @router.get("/{conversation_id}")
    def get_conversation(conversation_id: str, principal: RequestPrincipal = Depends(current_principal)):
        try:
            return require_storage().detail(conversation_id, principal.owner_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "conversation_not_found"}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"code": "conversation_not_found"}) from exc

    return router
