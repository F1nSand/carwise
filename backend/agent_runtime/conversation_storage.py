"""Owner-scoped conversation storage facade over the runtime repository."""

from __future__ import annotations

from typing import Any

from backend.agent_runtime.repositories import ConversationRepository


class ConversationStorage:
    def __init__(self, repository: ConversationRepository) -> None:
        self.repository = repository

    def create(self, owner_id: str, command_id: str) -> dict[str, Any]:
        return self.repository.create_conversation(owner_id, command_id)

    def list(self, owner_id: str, *, cursor: str | None = None, limit: int = 30) -> dict[str, Any]:
        return self.repository.list_conversations_owned(owner_id, cursor=cursor, limit=limit)

    def detail(self, conversation_id: str, owner_id: str) -> dict[str, Any]:
        return self.repository.get_conversation_owned(conversation_id, owner_id)

    def report(self, report_id: str, owner_id: str) -> dict[str, Any]:
        return self.repository.get_report_owned(report_id, owner_id)

    def send_message(self, conversation_id: str, owner_id: str, command_id: str, content: str,
                     source_report_id: str | None = None, *, allow_steering: bool = True,
                     admission_limits: dict[str, int] | None = None, reserve_tokens: int = 0) -> dict[str, Any]:
        return self.repository.accept_conversation_message(
            conversation_id, owner_id=owner_id, command_id=command_id, content=content,
            source_report_id=source_report_id, allow_steering=allow_steering,
            admission_limits=admission_limits, reserve_tokens=reserve_tokens,
        )

    def entries(self, conversation_id: str, owner_id: str, *, before_seq: int | None = None,
                limit: int = 50) -> dict[str, Any]:
        return self.repository.list_conversation_entries_owned(
            conversation_id, owner_id, before_seq=before_seq, limit=limit,
        )
