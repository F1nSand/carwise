"""Append-only JSON 会话存储和活动消息分支。

只保存动态 OpenAI 消息，不保存 system prompt、API key 或客户端对象。活动头
决定下一轮恢复哪些消息；回滚只移动活动头，旧记录仍保留以便审计和再次分支。
"""

from __future__ import annotations

import json
import os
import re
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.agent_runtime.state import _safe_value
from backend.config import ROOT

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_ALLOWED_ROLES = {"user", "assistant", "tool"}


def _validate_session_id(session_id: str) -> str:
    value = str(session_id or "").strip()
    if not _SESSION_ID.fullmatch(value):
        raise ValueError("session_id 必须是 1-128 位字母、数字、下划线或短横线")
    return value


@dataclass
class SessionSnapshot:
    session_id: str
    owner_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    active_head_id: str | None = None
    revision: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _safe_value(
            {
                "session_id": self.session_id,
                "owner_id": self.owner_id,
                "messages": self.messages,
                "active_head_id": self.active_head_id,
                "revision": self.revision,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "last_run_id": self.last_run_id,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], session_id: str) -> "SessionSnapshot":
        return cls(
            session_id=session_id,
            owner_id=str(data.get("owner_id")) if data.get("owner_id") else None,
            messages=list(data.get("messages") or []),
            active_head_id=data.get("active_head_id"),
            revision=int(data.get("revision") or 0),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            last_run_id=data.get("last_run_id"),
        )


class SessionStore:
    """进程安全的会话文件仓库；文件替换保证单次快照不会半写入。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else ROOT / ".cache" / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, session_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.RLock())

    def _path(self, session_id: str) -> Path:
        value = _validate_session_id(session_id)
        return self.root / f"{value}.json"

    def load(self, session_id: str) -> SessionSnapshot:
        value = _validate_session_id(session_id)
        path = self._path(value)
        with self._lock(value):
            if not path.exists():
                now = datetime.now(UTC).isoformat()
                return SessionSnapshot(value, created_at=now, updated_at=now)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("会话快照无法安全读取") from exc
            if not isinstance(data, dict):
                raise RuntimeError("会话快照格式非法")
            snapshot = SessionSnapshot.from_dict(data, value)
            self._validate_records(snapshot.messages)
            return snapshot

    def load_owned(self, session_id: str, owner_id: str) -> SessionSnapshot:
        value = _validate_session_id(session_id)
        with self._lock(value):
            if not self._path(value).exists():
                raise KeyError("会话不存在")
            snapshot = self.load(value)
            if not owner_id or str(snapshot.owner_id or "") != str(owner_id):
                raise KeyError("会话不存在")
            return snapshot

    def active_records(self, session_id: str) -> list[dict[str, Any]]:
        value = _validate_session_id(session_id)
        snapshot = self.load(value)
        by_id = {str(item.get("message_id")): item for item in snapshot.messages}
        current = snapshot.active_head_id
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        while current:
            if current in seen:
                raise RuntimeError("会话消息链存在循环")
            record = by_id.get(current)
            if record is None:
                raise RuntimeError("会话活动头指向不存在的消息")
            chain.append(deepcopy(record))
            seen.add(current)
            current = record.get("parent_id")
        chain.reverse()
        return chain

    def active_records_owned(self, session_id: str, owner_id: str) -> list[dict[str, Any]]:
        self.load_owned(session_id, owner_id)
        return self.active_records(session_id)

    def active_messages(self, session_id: str, owner_id: str | None = None) -> list[dict[str, Any]]:
        records = self.active_records_owned(session_id, owner_id) if owner_id is not None else self.active_records(session_id)
        return [self._public_message(record) for record in records]

    def append_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        run_id: str | None = None,
        owner_id: str | None = None,
        lease_token: str | None = None,
    ) -> SessionSnapshot:
        value = _validate_session_id(session_id)
        if not isinstance(messages, list):
            raise ValueError("messages 必须是列表")
        with self._lock(value):
            existed = self._path(value).exists()
            snapshot = self.load(value)
            if owner_id is not None:
                if snapshot.owner_id is not None and str(snapshot.owner_id) != str(owner_id):
                    raise KeyError("会话不存在")
                if existed and snapshot.owner_id is None:
                    raise KeyError("会话不存在")
                snapshot.owner_id = str(owner_id)
            if run_id and any(item.get("run_id") == str(run_id) for item in snapshot.messages):
                return snapshot
            parent_id = snapshot.active_head_id
            for message in messages:
                record = self._record_message(message, parent_id, run_id)
                snapshot.messages.append(record)
                parent_id = record["message_id"]
            if messages:
                snapshot.active_head_id = parent_id
                snapshot.revision += 1
                snapshot.last_run_id = str(run_id) if run_id else snapshot.last_run_id
            self._touch(snapshot)
            self._write(snapshot)
            return snapshot

    def rollback(
        self,
        session_id: str,
        message_id: str,
        expected_revision: int | None = None,
        owner_id: str | None = None,
    ) -> SessionSnapshot:
        value = _validate_session_id(session_id)
        target_id = str(message_id or "")
        with self._lock(value):
            snapshot = self.load(value)
            if owner_id is not None and str(snapshot.owner_id or "") != str(owner_id):
                raise KeyError("会话不存在")
            if expected_revision is not None and snapshot.revision != int(expected_revision):
                raise ValueError("会话 revision 已变化，请重新读取后再回滚")
            active = self.active_records(value)
            by_id = {str(item.get("message_id")): item for item in active}
            target = by_id.get(target_id)
            if target is None:
                raise ValueError("只能回滚到当前活动分支中的消息")
            snapshot.active_head_id = target.get("parent_id")
            snapshot.revision += 1
            self._touch(snapshot)
            self._write(snapshot)
            return snapshot

    @staticmethod
    def _public_message(record: dict[str, Any]) -> dict[str, Any]:
        message = {"role": record["role"], "content": record.get("content", "")}
        if record["role"] == "assistant" and "reasoning_content" in record:
            message["reasoning_content"] = deepcopy(record["reasoning_content"])
        if record["role"] == "assistant" and record.get("tool_calls"):
            message["tool_calls"] = deepcopy(record["tool_calls"])
        if record["role"] == "tool":
            message["tool_call_id"] = str(record.get("tool_call_id") or "")
            message["name"] = str(record.get("name") or "")
        return message

    @staticmethod
    def _record_message(
        message: dict[str, Any], parent_id: str | None, run_id: str | None
    ) -> dict[str, Any]:
        if not isinstance(message, dict):
            raise ValueError("消息必须是对象")
        role = str(message.get("role") or "")
        if role not in _ALLOWED_ROLES:
            raise ValueError(f"动态消息角色不支持：{role}")
        if role == "tool" and not message.get("tool_call_id"):
            raise ValueError("tool 消息必须包含 tool_call_id")
        record: dict[str, Any] = {
            "message_id": uuid4().hex,
            "parent_id": parent_id,
            "run_id": str(run_id) if run_id else None,
            "created_at": datetime.now(UTC).isoformat(),
            "role": role,
            "content": _safe_value(message.get("content", "")),
        }
        if role == "assistant" and "reasoning_content" in message:
            record["reasoning_content"] = _safe_value(message["reasoning_content"])
        if role == "assistant" and message.get("tool_calls"):
            record["tool_calls"] = _safe_value(message["tool_calls"])
        if role == "tool":
            record["tool_call_id"] = str(message["tool_call_id"])
            record["name"] = str(message.get("name") or "")
        return record

    @staticmethod
    def _validate_records(records: list[dict[str, Any]]) -> None:
        ids: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise RuntimeError("会话消息记录格式非法")
            message_id = str(record.get("message_id") or "")
            if not message_id or message_id in ids:
                raise RuntimeError("会话消息 ID 非法或重复")
            ids.add(message_id)
            if record.get("role") not in _ALLOWED_ROLES:
                raise RuntimeError("会话消息角色非法")

    @staticmethod
    def _touch(snapshot: SessionSnapshot) -> None:
        if not snapshot.created_at:
            snapshot.created_at = datetime.now(UTC).isoformat()
        snapshot.updated_at = datetime.now(UTC).isoformat()

    def _write(self, snapshot: SessionSnapshot) -> None:
        path = self._path(snapshot.session_id)
        tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(
                json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError("会话快照写入失败") from exc
