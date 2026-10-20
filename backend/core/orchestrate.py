"""应用编排入口：所有请求直接进入单 Agent 原生 tool-call loop。"""

from __future__ import annotations

from backend.agent_runtime.loop import run_agent
from backend.agent_runtime.session_store import SessionStore


def run_query(
    text: str,
    session_id: str | None = None,
    run_id: str | None = None,
    session_store: SessionStore | None = None,
) -> dict:
    """执行一次单 Agent 查询。"""
    return run_agent(text, session_id=session_id, run_id=run_id, session_store=session_store)
