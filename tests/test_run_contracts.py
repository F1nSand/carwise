import pytest

from backend.agent_runtime.lifecycle import outcome_from_snapshot
from backend.agent_runtime.run_contracts import (
    RunContractError,
    validate_snapshot,
    validate_transition,
)


def test_snapshot_contract_accepts_waiting_and_completed_outcomes():
    waiting = {
        "run_id": "run-waiting",
        "status": "waiting_user",
        "checkpoint": {"checkpoint_version": 2},
        "pending_input": {
            "tool_call_id": "ask-budget",
            "questions": [{"id": "budget", "prompt": "预算是多少？"}],
        },
    }
    done = {"run_id": "run-done", "status": "done", "result": {"summary": "已完成推荐"}}

    assert outcome_from_snapshot(waiting).kind == "waiting"
    assert outcome_from_snapshot(done).kind == "completed"


@pytest.mark.parametrize(
    ("snapshot", "error"),
    [
        ({"status": "waiting_user", "checkpoint": {}, "pending_input": {"questions": [{"id": "budget"}]}}, "tool_call_id"),
        ({"status": "waiting_user", "checkpoint": {}, "pending_input": {"tool_call_id": "ask", "questions": []}}, "questions"),
        ({"status": "waiting_user", "pending_input": {"tool_call_id": "ask", "questions": [{"id": "budget"}]}}, "checkpoint"),
        ({"status": "done", "result": None}, "completed_outcome_missing_result"),
        ({"status": "done", "result": {"summary": "完成"}, "pending_input": {"tool_call_id": "ask"}}, "done_snapshot_has_pending_input"),
        ({"status": "error"}, "error_snapshot_missing_failure"),
        ({"status": "unknown"}, "unknown_run_status"),
    ],
)
def test_snapshot_contract_rejects_incomplete_or_conflicting_states(snapshot, error):
    with pytest.raises(RunContractError, match=error):
        validate_snapshot(snapshot)


def test_run_transition_contract_rejects_terminal_reopen():
    validate_transition("running", "waiting_user")
    validate_transition("waiting_user", "queued")
    with pytest.raises(RunContractError, match="illegal_run_transition:done->running"):
        validate_transition("done", "running")
