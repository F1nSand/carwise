"""Agent 运行轨迹：只记录受信代码观察到的事件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from backend.agent_runtime.state import _safe_value


@dataclass
class RunTrace:
    trace_id: str
    run_id: str = ""
    segment_id: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(cls, trace_id: str | None = None, run_id: str | None = None,
               segment_id: str | None = None) -> "RunTrace":
        return cls(trace_id=trace_id or uuid4().hex, run_id=str(run_id or ""),
                   segment_id=str(segment_id or uuid4().hex))

    def record(
        self,
        node: str,
        event_type: str,
        input_data: Any,
        output_data: Any,
        status: str,
        duration_ms: int = 0,
        step: int | None = None,
        constraint_revision: int | None = None,
        attempt: int | None = None,
    ) -> None:
        event = {
                "event_id": uuid4().hex,
                "run_id": self.run_id,
                "segment_id": self.segment_id,
                "node": str(node),
                "event_type": str(event_type),
                "input": _safe_value(input_data),
                "output": _safe_value(output_data),
                "status": str(status),
                "duration_ms": max(0, int(duration_ms)),
            }
        if step is not None:
            event["step"] = int(step)
            if str(event_type) == "model_round":
                event["model_round"] = int(step)
        event_input = input_data if isinstance(input_data, dict) else {}
        if event_input.get("tool_call_id") is not None:
            event["tool_call_id"] = str(event_input["tool_call_id"])
        if attempt is not None:
            event["attempt"] = max(1, int(attempt))
        if constraint_revision is not None:
            event["constraint_revision"] = max(0, int(constraint_revision))
        revision = event_input.get("constraint_revision")
        if constraint_revision is None and isinstance(revision, int) and not isinstance(revision, bool):
            event["constraint_revision"] = revision
        self.events.append(event)

    def to_dict(self) -> dict[str, Any]:
        return _safe_value(
            {"trace_id": self.trace_id, "run_id": self.run_id,
             "segment_id": self.segment_id, "events": self.events}
        )
