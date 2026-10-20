from backend.api_projection import public_run_event, public_run_snapshot, public_terminal_result
import pytest


@pytest.mark.parametrize('status', ['queued', 'running', 'done', 'error'])
def test_non_waiting_snapshot_does_not_publish_old_checkpoint_question(status):
    pending = {'tool_call_id': 'ask-answered', 'questions': []}
    snapshot = public_run_snapshot({
        'status': status, 'resume_revision': 1, 'attempt': 2,
        'pending_input': pending, 'runtime': {'pending_input': pending},
        'checkpoint': {'pending_input': pending},
    })
    assert 'pending_input' not in snapshot
    assert 'pending_input' not in snapshot['runtime']


def test_accepted_wait_is_queued_publicly_before_worker_claims_it():
    raw = {'status': 'waiting_user', 'request': {'resume_ready': True},
           'pending_input': {'tool_call_id': 'answered', 'questions': []}}
    public = public_run_snapshot(raw)
    assert public['status'] == public['runtime']['status'] == 'queued'
    assert 'pending_input' not in public
    assert 'pending_input' not in public['runtime']
    assert raw['status'] == 'waiting_user'


def test_waiting_snapshot_uses_run_revision_for_public_question_barrier():
    pending = {"tool_call_id": "ask", "step": 1, "resume_revision": 1, "questions": []}
    snapshot = public_run_snapshot({"status": "waiting_user", "resume_revision": 0,
                                    "pending_input": pending})
    assert snapshot["pending_input"]["resume_revision"] == 0
    assert snapshot["runtime"]["pending_input"]["resume_revision"] == 0
    assert pending["resume_revision"] == 1  # Agent's internal question counter stays intact.


def test_waiting_event_uses_run_revision_for_public_question_barrier():
    event = public_run_event({"event_type": "input_required", "resume_revision": 0,
                              "pending_input": {"tool_call_id": "ask", "resume_revision": 1, "questions": []}})
    assert event["resume_revision"] == event["pending_input"]["resume_revision"] == 0


def test_run_snapshot_exposes_only_runtime_resume_fields():
    snapshot = public_run_snapshot(
        {
            "run_id": "run-1",
            "session_id": "session-1",
            "status": "running",
            "next_seq": 8,
            "recoverable": True,
            "recovery_reason": "可从检查点恢复",
            "input_revision": 4,
            "plan_revision": 2,
            "capabilities": {"midrun_input": True, "recover": True},
            "input": "预算改为 12 万",
            "checkpoint": {"private_checkpoint": "secret", "pending_input": {"questions": []}},
            "runtime": {
                "status": "running",
                "plan": [{"id": "query", "title": "查询", "description": "筛选车源", "tool_names": ["recommend"], "status": "running"}],
                "activities": [
                    {
                        "seq": 7,
                        "event_type": "tool_rejected",
                        "input": {"tool_call_id": "call-1", "tool_name": "recommend", "arguments": "secret"},
                        "output": {"ok": False, "error_code": "constraint_changed", "private_result": "secret"},
                        "status": "rejected",
                    }
                ],
            },
        }
    )

    assert snapshot["last_event_seq"] == 7
    assert snapshot["recoverable"] is True
    assert snapshot["input_revision"] == 4
    assert snapshot["plan_revision"] == 2
    assert snapshot["capabilities"] == {"midrun_input": True, "recover": True}
    assert snapshot["runtime"]["last_event_seq"] == 7
    assert snapshot["runtime"]["input_revision"] == 4
    assert snapshot["runtime"]["capabilities"] == {"midrun_input": True, "recover": True}
    assert snapshot["runtime"]["activities"][0]["event_type"] == "tool_rejected"
    serialized = str(snapshot)
    assert "private_checkpoint" not in serialized
    assert '"arguments"' not in serialized
    assert "private_result" not in serialized


def test_run_snapshot_does_not_treat_boolean_counters_as_integers():
    snapshot = public_run_snapshot({"next_seq": True, "input_revision": True, "plan_revision": True})

    assert "last_event_seq" not in snapshot
    assert "input_revision" not in snapshot
    assert "plan_revision" not in snapshot


def test_run_snapshot_accepts_nested_runtime_resume_metadata():
    snapshot = public_run_snapshot(
        {
            "run_id": "run-2",
            "status": "waiting_user",
            "runtime": {
                "status": "waiting_user",
                "last_event_seq": 11,
                "input_revision": 6,
                "plan_revision": 4,
                "capabilities": {"midrun_input": True, "recover": False},
                "pending_input": {"tool_call_id": "call-ask", "step": 1, "questions": []},
            },
        }
    )

    assert snapshot["last_event_seq"] == 11
    assert snapshot["input_revision"] == 6
    assert snapshot["plan_revision"] == 4
    assert snapshot["capabilities"] == {"midrun_input": True, "recover": False}
    assert snapshot["runtime"]["pending_input"]["tool_call_id"] == "call-ask"


def test_run_snapshot_exposes_task_mode_goals_and_plan_goal_bindings():
    snapshot = public_run_snapshot({
        "status": "running",
        "runtime": {
            "status": "running",
            "task_intent": {
                "revision": 2,
                "mode": "planned",
                "needs_plan": True,
                "source": "internal",
                "goals": [{
                    "id": "compare",
                    "description": "比较两款车",
                    "deliverable": "价格对比",
                    "depends_on": [],
                }],
            },
            "plan": [{
                "id": "query",
                "title": "查价格",
                "description": "查询两款车",
                "tool_names": ["price_compare"],
                "goal_ids": ["compare"],
                "status": "running",
            }],
        },
    })

    assert snapshot["runtime"]["task_mode"] == "planned"
    assert snapshot["runtime"]["task_intent"]["revision"] == 2
    assert snapshot["runtime"]["task_intent"]["goals"][0]["deliverable"] == "价格对比"
    assert snapshot["runtime"]["plan"][0]["goal_ids"] == ["compare"]
    assert "source" not in str(snapshot)


def test_checkpoint_projection_exposes_complete_plan_identity_and_coverage_watermark():
    snapshot = public_run_snapshot({
        "run_id": "run-projection-v3",
        "attempt": 2,
        "state_revision": 9,
        "covered_event_seq": 14,
        "next_seq": 16,
        "status": "running",
        "checkpoint": {
            "messages": [{"role": "assistant", "content": "private"}],
            "tool_results": [{"output": {"secret": "private"}}],
            "task_intent": {
                "revision": 3,
                "mode": "planned",
                "needs_plan": True,
                "source": "internal",
                "goals": [{"id": "g1", "description": "查价并整理", "deliverable": "价格结论", "depends_on": []}],
            },
            "flags": {"plan_revision": 4, "input_revision": 2},
            "plan": [
                {"id": "query", "title": "查询价格", "description": "读取匹配样本", "tool_names": ["price_compare"],
                 "goal_ids": ["g1"], "kind": "data", "completion_criteria": "tool_result", "status": "complete"},
                {"id": "finalize", "title": "整理并核验", "description": "提交最终回答", "tool_names": ["finalize_answer"],
                 "goal_ids": ["g1"], "kind": "finalize", "completion_criteria": "validated_answer", "status": "running"},
            ],
            "run_logs": [],
        },
        "runtime": {"capabilities": {"midrun_input": True, "recover": True}},
    })

    assert snapshot["protocol_version"] == 3
    assert snapshot["attempt"] == 2
    assert snapshot["state_revision"] == 9
    assert snapshot["last_event_seq"] == 15
    assert snapshot["covered_event_seq"] == 14
    assert snapshot["task_revision"] == 3
    assert snapshot["plan_revision"] == 4
    assert snapshot["runtime"]["current_stage"] == {"id": "finalize", "title": "整理并核验"}
    assert snapshot["runtime"]["plan"][1]["kind"] == "finalize"
    assert snapshot["runtime"]["plan"][1]["completion_criteria"] == "validated_answer"
    serialized = str(snapshot)
    assert "private" not in serialized
    assert "tool_results" not in serialized


def test_task_intent_event_is_projected_without_internal_fields():
    event = public_run_event({
        "seq": 4,
        "event_type": "task_intent_update",
        "output": {
            "task_intent": {
                "revision": 1,
                "mode": "simple",
                "needs_plan": False,
                "source": "internal",
                "goals": [{
                    "id": "query",
                    "description": "查价格",
                    "deliverable": "价格分布",
                    "depends_on": [],
                }],
            },
        },
    })

    assert event["event_type"] == "task_intent_update"
    assert event["output"]["task_intent"]["mode"] == "simple"
    assert "source" not in str(event)


def test_runtime_event_includes_attempt_scoped_identity():
    event = public_run_event({
        "run_id": "run-identity",
        "seq": 7,
        "event_seq": 7,
        "attempt": 3,
        "state_revision": 8,
        "event_type": "plan_update",
        "output": {"steps": [{"id": "finish", "title": "核验", "kind": "finalize", "completion_criteria": "validated_answer", "status": "running"}], "revision": 4},
    })

    assert event["run_id"] == "run-identity"
    assert event["event_seq"] == event["seq"] == 7
    assert event["attempt"] == 3
    assert event["state_revision"] == 8
    assert event["output"]["steps"][0]["kind"] == "finalize"


def test_terminal_result_retains_a_public_final_plan():
    analysis_blocks = [{"analysis_type": "price_distribution", "dataset_version": "v1", "data": {"n": 8}}]
    result = public_terminal_result({
        "status": "done",
        "run_id": "run-done-plan",
        "report": {"id": "report-1", "summaryText": "完成", "analysisBlocks": analysis_blocks, "internal": "drop"},
        "state": {
            "messages": [{"role": "assistant", "content": "internal"}],
            "plan": [{"id": "query", "title": "查询", "description": "取数", "tool_names": ["price_compare"],
                      "goal_ids": ["g1"], "kind": "data", "completion_criteria": "tool_result", "status": "complete"}],
        },
    })

    assert result["report"]["runId"] == "run-done-plan"
    assert result["report"]["finalPlan"][0]["id"] == "query"
    assert result["report"]["analysisBlocks"] == analysis_blocks
    assert "internal" not in str(result)


def test_done_projection_exposes_only_stable_conversation_references():
    event = public_run_event({
        "event_type": "done",
        "report_ref": {"report_id": "report-1", "version": 2, "origin_run_id": "run-1",
                       "conversation_id": "conversation-1", "private": "drop"},
        "entry_ids": {"assistant_entry_id": "entry-a", "report_entry_id": "entry-r", "secret": "drop"},
        "result": {"status": "done", "summary": "回答", "report_ref": {
            "report_id": "report-1", "version": 2, "origin_run_id": "run-1",
            "conversation_id": "conversation-1", "private": "drop"},
            "entry_ids": {"assistant_entry_id": "entry-a", "report_entry_id": "entry-r", "secret": "drop"}},
    })
    assert event["report_ref"] == {"report_id": "report-1", "version": 2,
                                   "origin_run_id": "run-1", "conversation_id": "conversation-1"}
    assert event["entry_ids"] == {"assistant_entry_id": "entry-a", "report_entry_id": "entry-r"}
    assert event["result"]["report_ref"] == event["report_ref"]
    assert "secret" not in str(event)
