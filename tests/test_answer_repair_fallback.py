from backend.agent_runtime.answer_validation import (
    ValidationResult,
    deterministic_answer_from_evidence,
    record_answer_validation_failure,
)
from backend.agent_runtime.nodes import _consume_answer_repair
from backend.agent_runtime.loop import _record_finalize_protocol_reminder
from backend.agent_runtime.run_budget import new_run_budget
from backend.agent_runtime.state import AgentState


def _state(monkeypatch):
    from backend import data_loader

    monkeypatch.setattr(data_loader, "get_dataset_version", lambda: "dataset-current")
    state = AgentState.create("价格分布", "system")
    state.task_intent = {"goals": [{"id": "price", "description": "挂牌价分布"}]}
    state.task_context = {"revision": 4}
    state.plan = [
        {"id": "query", "kind": "data", "goal_ids": ["price"], "tool_names": ["price_compare", "recommend"], "status": "complete"},
        {"id": "final", "kind": "finalize", "goal_ids": ["price"], "status": "running"},
    ]
    state.run_budget = new_run_budget()
    state.tool_results = [{
        "tool_call_id": "price-call", "tool_name": "price_compare", "status": "done",
        "plan_step_id": "query", "goal_ids": ["price"], "constraint_revision": 4,
        "output": {"ok": True},
    }]
    return state


def _price_records():
    records = []
    for metric, value, unit in (
        ("price.p25_yuan", 198000, "CNY"),
        ("price.median_yuan", 209800, "CNY"),
        ("price.p75_yuan", 225000, "CNY"),
        ("price.mean_yuan", 211576.25, "CNY"),
        ("sample_size", 261, "count"),
    ):
        records.append({
            "evidence_id": f"ev-{metric}", "tool_call_id": "price-call",
            "dataset_version": "dataset-current", "constraint_revision": 4,
            "metric": metric, "value": value, "unit": unit, "filters": ["车系 = 奥迪 A4L"],
            "sample_size": 261, "scope": "population", "goal_ids": ["price"],
        })
    return records


def test_answer_diagnostics_keep_all_deduplicated_issues_and_fingerprint():
    state = AgentState.create("task", "system")
    issues = [
        {"code": "unknown_evidence", "path": "blocks[0]", "category": "reference", "message": "missing ref", "repair_hint": "use valid ref", "expected_refs": ["ev-current"]},
        {"code": "unknown_evidence", "path": "blocks[0]", "category": "reference", "message": "missing ref", "repair_hint": "use valid ref"},
        {"code": "goal_without_evidence", "path": "blocks", "category": "coverage", "message": "goal missing", "repair_hint": "cover goal"},
    ]
    record = record_answer_validation_failure(
        state, {"candidate": 1}, ValidationResult(False, ["missing ref", "goal missing"], issues=issues), phase="preflight",
    )
    assert len(record["issues"]) == 2
    assert record["issues"][0]["expected_refs"] == ["ev-current"]
    assert record["candidate_fingerprint"]
    repeated = record_answer_validation_failure(
        state, {"candidate": 1}, ValidationResult(False, ["missing ref"], issues=issues[:1]), phase="preflight",
    )
    assert repeated["candidate_fingerprint"] == record["candidate_fingerprint"]
    assert state.flags["answer_no_progress"] is True


def test_repair_budget_exhaustion_preserves_diagnostic_and_fingerprint():
    state = AgentState.create("task", "system")
    state.run_budget = new_run_budget({"answer_repairs": 1})
    draft = {"schema_version": 2, "blocks": []}
    validation = ValidationResult(False, ["bad schema"], issues=[{
        "code": "blocks", "path": "blocks", "category": "schema", "message": "bad schema", "repair_hint": "add block",
    }])
    record_answer_validation_failure(state, draft, validation, phase="preflight")
    output = {}
    _consume_answer_repair(state, draft, validation, output, phase="preflight")
    assert state.run_budget["answer_repairs"] == 1
    # The same draft makes no progress and stops without spending another repair.
    record_answer_validation_failure(state, draft, validation, phase="preflight")
    output = {}
    _consume_answer_repair(state, draft, validation, output, phase="preflight")
    assert output["error_code"] == "answer_repair_exhausted"
    assert output["answer_validation"]["issues"][0]["message"] == "bad schema"
    assert output["attempts"] == 2
    assert output["candidate_fingerprint"]
    assert output["repair_budget"]["remaining"] == 0
    assert state.flags["answer_schema_repairs"] == 2


def test_missing_finalize_reminder_is_counted_outside_answer_repair_budget():
    state = AgentState.create("说明能力范围", "system")
    state.run_budget = new_run_budget({"answer_repairs": 2})

    _record_finalize_protocol_reminder(state)
    _record_finalize_protocol_reminder(state)

    assert state.flags["finalize_protocol_reminders"] == 2
    assert state.run_budget["answer_repairs"] == 0


def test_complete_fresh_price_evidence_produces_validated_distribution_fallback(monkeypatch):
    state = _state(monkeypatch)
    state.evidence_records = _price_records()
    result = deterministic_answer_from_evidence(state)
    assert result is not None and result.valid, result.errors if result else None
    assert "25 分位" in result.answer
    assert "中位数" in result.answer
    assert "75 分位" in result.answer
    assert "209800元" in result.answer
    assert "211576元" in result.answer
    assert "261条" in result.answer


def test_complete_fresh_empty_results_produce_validated_no_data_fallback(monkeypatch):
    state = _state(monkeypatch)
    state.evidence_records = []
    state.tool_results[0]["dataset_version"] = "dataset-current"
    state.tool_results[0]["output"] = {
        "ok": True, "status": "empty", "result_status": "empty", "reason_code": "no_data",
    }

    result = deterministic_answer_from_evidence(state)

    assert result is not None and result.valid, result.errors if result else None
    assert "没有可用于回答的数据" in result.answer
    assert "数值结论" in result.answer


def test_stale_empty_result_cannot_produce_no_data_fallback(monkeypatch):
    state = _state(monkeypatch)
    state.evidence_records = []
    state.tool_results[0]["dataset_version"] = "dataset-old"
    state.tool_results[0]["output"] = {
        "ok": True, "status": "empty", "result_status": "empty", "reason_code": "no_data",
    }

    assert deterministic_answer_from_evidence(state) is None
    assert state.flags["answer_fallback_gaps"][0]["code"] == "stale_or_unbound_evidence"


def test_stale_evidence_or_incomplete_recommendation_cannot_fallback(monkeypatch):
    state = _state(monkeypatch)
    stale = _price_records()
    stale[0]["dataset_version"] = "dataset-old"
    state.evidence_records = stale
    assert deterministic_answer_from_evidence(state) is None
    assert state.flags["answer_fallback_gaps"][0]["code"] == "stale_or_unbound_evidence"

    state = _state(monkeypatch)
    state.tool_results[0]["tool_name"] = "recommend"
    state.evidence_records = [{
        "evidence_id": "ev-rec-median", "tool_call_id": "price-call",
        "dataset_version": "dataset-current", "constraint_revision": 4,
        "metric": "recommendations.0.median_wan", "value": 19.8, "unit": "wan",
        "entity_group_id": "ent-one", "entity_label": "示例车系", "sample_size": 12,
        "scope": "population", "goal_ids": ["price"],
    }]
    assert deterministic_answer_from_evidence(state) is None
    assert state.flags["answer_fallback_gaps"][0]["code"] == "recommendation_group_incomplete"


def test_complete_recommendation_fallback_keeps_names_bound_to_prices_and_samples(monkeypatch):
    state = _state(monkeypatch)
    state.tool_results[0]["tool_name"] = "recommend"
    records = []
    for group_id, series, median in (("ent-a", "深蓝 SL03", 15.8), ("ent-b", "零跑 C11", 18.2)):
        for suffix, metric, value, unit in (
            ("median", "median_wan", median, "wan"),
            ("low", "p25_wan", median - 1, "wan"),
            ("high", "p75_wan", median + 1, "wan"),
            ("size", "sample_size", 24, "count"),
        ):
            records.append({
                "evidence_id": f"ev-{group_id}-{suffix}", "tool_call_id": "price-call",
                "dataset_version": "dataset-current", "constraint_revision": 4,
                "metric": f"recommendations.{group_id}.{metric}", "value": value, "unit": unit,
                "entity_group_id": group_id, "entity_label": series, "sample_size": 24,
                "scope": "population", "goal_ids": ["price"],
            })
    state.evidence_records = records
    validation = deterministic_answer_from_evidence(state)
    assert validation is not None and validation.valid, validation.errors if validation else None
    assert "深蓝 SL03" in validation.answer and "15.80万元" in validation.answer and "24 条" in validation.answer
    assert "零跑 C11" in validation.answer and "18.20万元" in validation.answer


def test_fallback_closes_through_normal_finalize_completion_contract(monkeypatch):
    from backend.agent_runtime.completion_policy import plan_ready_for_completion
    from backend.agent_runtime.loop import _apply_deterministic_answer_fallback

    state = _state(monkeypatch)
    state.evidence_records = _price_records()
    state.flags["answer_validation"] = {"valid": False, "candidate_fingerprint": "bad-draft"}
    validation = deterministic_answer_from_evidence(state)
    assert _apply_deterministic_answer_fallback(state, validation)
    assert state.flags["validated_answer"] == validation.answer
    assert state.tool_results[-1]["tool_name"] == "finalize_answer"
    assert state.tool_results[-1]["output"]["answer_validation"]["valid"] is True
    assert plan_ready_for_completion(state)
