import json
from pathlib import Path

from backend.evals.models import EvalResult, parse_case
from backend.evals.reporting import aggregate_results, compare_baseline, write_report
from backend.evals.runner import evaluate_case
from backend.evals.scorers import score_run


def test_trajectory_protocol_rejects_stale_assets(tmp_path):
    import pytest
    from backend.evals.runner import _load_trajectory
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"version": 1, "responses": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="protocol version"):
        _load_trajectory(path)


def test_repository_trajectories_follow_current_protocol():
    from backend.evals.runner import _load_trajectory, _trajectory_path
    from backend.evals.models import load_cases
    for case in load_cases(Path("evals/cases/v1.jsonl")):
        if case.trajectory_fixture:
            assert _load_trajectory(_trajectory_path(case.trajectory_fixture))


def test_offline_evaluation_cannot_bypass_required_plan():
    from dataclasses import replace
    from backend.evals.models import load_cases
    from backend.evals.runner import _load_trajectory, _trajectory_path
    case = next(item for item in load_cases(Path("evals/cases/v1.jsonl")) if item.id == "price_comparison-01")
    responses = _load_trajectory(_trajectory_path(case.trajectory_fixture))
    responses[0]["tool_calls"] = [call for call in responses[0]["tool_calls"] if call["name"] != "set_plan"]
    invalid = replace(case, trajectory_fixture=None, trajectory=tuple(responses))
    result = evaluate_case(invalid, mode="trajectory")[0]
    assert not result.passed
    assert any(item.get("output", {}).get("error_code") == "plan_required" for item in result.run.get("tool_results", []))


def test_unsupported_filter_scorer_requires_actionable_confirmation():
    case = parse_case({"id": "unsupported-pause", "category": "unsupported_filters",
                       "turns": [{"role": "user", "content": "SUV"}],
                       "expect": {"status": "waiting_user", "pending_decision_reason": "unsupported_filter"}})
    assert not score_run(case, {"status": "waiting_user"}).passed
    assert score_run(case, {"status": "waiting_user", "state": {"flags": {"pending_user_decision": {"reason_code": "unsupported_filter"}}},
                            "checkpoint": {"pending_input": {"questions": [{"id": "body"}]}}}).passed


def test_replay_runner_isolates_run_ids_and_writes_reports(tmp_path, monkeypatch):
    case = parse_case({"id": "runner-1", "category": "price_comparison", "turns": [{"role": "user", "content": "价格"}], "expect": {"status": "done"}})
    calls = []

    def fake_runner(text, *, run_id, session_id):
        calls.append((text, run_id, session_id))
        return {"status": "done", "summary": "完成"}

    results = evaluate_case(case, mode="replay", repetitions=2, runner=fake_runner)
    assert len(results) == 2 and all(item.passed for item in results)
    assert calls[0][1] != calls[1][1]
    summary = write_report(results, tmp_path)
    assert summary["pass_rate"] == 1.0
    assert (tmp_path / "summary.json").exists()
    assert json.loads((tmp_path / "summary.json").read_text())[
        "case_count"
    ] == 2


def test_baseline_thresholds_flag_regressions():
    violations = compare_baseline(
        {"pass_rate": 1.0, "scores": {"critical": 1.0}, "avg_tokens": 100},
        {"pass_rate": 0.9, "scores": {"critical": 0.9}, "avg_tokens": 120, "hard_violations": 1},
    )
    assert len(violations) >= 3


def test_evidence_availability_rate_counts_only_domain_data_tools():
    result = EvalResult(
        case_id="evidence-rate",
        passed=True,
        scores={},
        violations=(),
        run={
            "status": "done",
            "tool_results": [
                {"tool_name": "price_compare", "output": {"evidence": [{"evidence_id": "ev-one"}]}},
                {"tool_name": "recommend", "output": {"evidence": []}},
                {"tool_name": "finalize_answer", "output": {"evidence": []}},
                {"tool_name": "ask_user", "output": {"evidence": []}},
            ],
        },
    )

    summary = aggregate_results([result])

    assert summary["evidence_pass_rate"] == 0.5


def test_trajectory_replays_actual_loop_pause_answer_and_tool_call_offline():
    case = parse_case({
        "id": "runner-trajectory", "category": "clarification",
        "turns": [
            {"role": "user", "action": "user_input", "content": "帮我找二手车"},
            {"role": "user", "action": "answer", "content": "15万", "answers": [{"question_id": "budget", "value": "15万"}]},
        ],
        "expect": {"status": "done", "required_tools": ["ask_user", "price_compare"]},
        "trajectory": [
            {"tool_calls": [
                {"id": "declare-search", "name": "declare_task", "arguments": {"goals": [{"id": "price", "description": "Find a used car and confirm its price", "deliverable": "Price analysis"}], "requires_plan": False}},
                {"id": "plan-search", "name": "set_plan", "arguments": {"steps": [
                    {"id": "clarify", "title": "确认预算", "description": "询问用户确认预算上限", "tool_names": ["ask_user"], "goal_ids": ["price"], "kind": "clarification", "completion_criteria": "user_answer"},
                    {"id": "query", "title": "查询参考价格", "description": "按确认的预算查询候选行情", "tool_names": ["price_compare"], "goal_ids": ["price"], "kind": "data", "completion_criteria": "tool_result"},
                    {"id": "finalize", "title": "整理并核验回答", "description": "依据本次证据校验并交付结论", "tool_names": ["finalize_answer"], "goal_ids": ["price"], "kind": "finalize", "completion_criteria": "validated_answer"}
                ]}},
                {"id": "ask-budget", "name": "ask_user", "arguments": {"questions": [{"id": "budget", "prompt": "预算上限？", "options": [{"id": "15w", "label": "15万", "value": "15万"}], "allow_custom": True}]}}
            ]},
            {"tool_calls": [{"id": "price-after-answer", "name": "price_compare", "arguments": {"series": "宝马3系"}}]},
            {"tool_calls": [
                {"id": "close-query", "name": "update_plan", "arguments": {"updates": [
                    {"id": "query", "status": "complete"}, {"id": "finalize", "status": "running"}
                ]}},
                {
                    "id": "final-answer",
                    "name": "finalize_answer",
                    "arguments": {
                        "schema_version": 2,
                        "result_kind": "data_answer",
                        "goal_ids": ["price"],
                        "blocks": [
                            {"kind": "text", "text": "根据本次查询，"},
                            {"kind": "fact_ref", "evidence_id": {"$evidence_ref": {"metric": "price.median_yuan"}}, "format": "sentence"}
                        ],
                        "limitation_codes": ["listing_price_not_transaction"],
                    },
                },
            ]},
        ],
    })
    result = evaluate_case(case, mode="trajectory")[0]
    assert result.passed, result.violations
    assert result.model_rounds == 3
    assert "15万" in json.dumps(result.run["model_request_messages"], ensure_ascii=False)
    finalizer = next(item for item in result.run["tool_results"] if item.get("tool_name") == "finalize_answer")
    assert finalizer["output"]["draft"]["schema_version"] == 2
    assert finalizer["output"]["draft"]["blocks"][1]["evidence_id"].startswith("ev_")
    assert not result.run["trajectory_remaining"]


def test_fixture_mode_does_not_fall_back_to_live_runner():
    case = parse_case({"id": "no-fixture-999", "category": "price_comparison", "turns": [{"role": "user", "content": "价格"}], "expect": {"status": "done"}})
    called = []
    result = evaluate_case(case, mode="fixture", runner=lambda *args, **kwargs: called.append(True))[0]
    assert not result.passed
    assert not called


def test_plan_requirements_validate_dynamic_titles_and_required_stages():
    case = parse_case({
        "id": "dynamic-plan-shape",
        "category": "price_comparison",
        "turns": [{"role": "user", "content": "查询价格"}],
        "expect": {
            "status": "done",
            "plan_complete": True,
            "plan_requirements": {
                "min_steps": 2,
                "required_kinds": ["data", "finalize"],
                "titles_nonempty": True,
            },
        },
    })
    dynamic_run = {
        "status": "done",
        "state": {"plan": [
            {"title": "检索当前市场样本", "kind": "data", "status": "complete"},
            {"title": "整理并核对结论", "kind": "finalize", "status": "complete"},
        ]},
    }
    assert score_run(case, dynamic_run).passed

    missing_finalize = {
        "status": "done",
        "state": {"plan": [
            {"title": "检索当前市场样本", "kind": "data", "status": "complete"},
        ]},
    }
    result = score_run(case, missing_finalize)
    assert not result.passed
    assert "plan_requirements: missing step kind finalize" in result.violations


def test_answer_scorers_inspect_the_delivered_answer_before_raw_model_text():
    case = parse_case({
        "id": "delivered-answer-source",
        "category": "price_comparison",
        "turns": [{"role": "user", "content": "A4L 价格"}],
        "expect": {"status": "done", "summary_contains": ["A4L"]},
    })
    result = score_run(case, {
        "status": "done",
        "summary": "奥迪 A4L 的挂牌价统计如下。",
        "final_text": "原始模型草稿没有实体名称。",
    })
    assert result.passed, result.violations
