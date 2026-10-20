import threading
import time

import pytest

from backend.agent_runtime.execution import ExecutionBoundaryError, ExecutionContext, check_execution


def test_execution_context_enforces_cancel_and_monotonic_deadline():
    cancel = threading.Event()
    context = ExecutionContext("run", "segment", cancel, time.monotonic() + 5)
    check_execution(context)
    cancel.set()
    with pytest.raises(ExecutionBoundaryError, match="取消") as cancelled:
        check_execution(context)
    assert cancelled.value.code == "cancelled"

    expired = ExecutionContext("run", "segment", threading.Event(), time.monotonic() - 1)
    with pytest.raises(ExecutionBoundaryError) as timeout:
        check_execution(expired)
    assert timeout.value.code == "deadline_exceeded"
