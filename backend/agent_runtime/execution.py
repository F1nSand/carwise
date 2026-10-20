"""Per-segment cancellation and monotonic deadline checks."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class ExecutionBoundaryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExecutionContext:
    run_id: str
    segment_id: str
    cancellation: threading.Event
    deadline_monotonic: float

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())


def check_execution(context: ExecutionContext | None) -> None:
    if context is None:
        return
    if context.cancellation.is_set():
        raise ExecutionBoundaryError("cancelled", "Agent 运行已取消。")
    if context.remaining_seconds() <= 0:
        raise ExecutionBoundaryError("deadline_exceeded", "Agent 执行片段已超时。")
