"""Offline regression fixtures for Agent planning and evidence-backed delivery."""

from __future__ import annotations

import json
from pathlib import Path

from backend.agent_runtime.answer_validation import ValidationResult, validate_answer_draft
from backend.agent_runtime.context_budget import ContextBudget, TokenEstimator, build_context_window
from backend.agent_runtime.evidence import evidence_from_tool_result
from backend.agent_runtime.state import AgentState
from backend.agent_runtime.task_context import normalize_task_intent
from backend.agent_runtime.tool_policy import Reject, preflight_tool_call
from backend.evals.models import parse_case


ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "fixtures" / "agent_reliability" / "2026-10-05"
TRAJECTORY_DIR = ROOT.parent / "evals" / "trajectories" / "reliability"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _has_forbidden_fixture_key(value: object) -> bool:
    forbidden = {
        "run_id", "session_id", "context_id", "owner_id", "trace_id",
        "tool_call_id", "lease_token", "api_key", "authorization",
    }
    if isinstance(value, dict):
        return any(str(key).casefold() in forbidden or _has_forbidden_fixture_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_has_forbidden_fixture_key(item) for item in value)
    return False


def test_reliability_replays_are_redacted_and_use_current_scripted_responses():
    fixture_files = sorted(FIXTURE_DIR.glob("*.json"))
    assert {path.name for path in fixture_files} == {
        "price_a4l_answer_invalid.json",
        "recommend_entity_loss.json",
        "recommend_filter_answer_invalid.json",
    }
    for path in fixture_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["fixture_version"] == 1
        assert not _has_forbidden_fixture_key(payload), path.name
        assert "C:\\Users\\" not in path.read_text(encoding="utf-8")

    trajectory_names = {
        "price_a4l_plan_answer.json",
        "recommend_filters_plan_answer.json",
        "recommend_named_entities.json",
    }
    trajectory_files = [TRAJECTORY_DIR / name for name in sorted(trajectory_names)]
    assert all(path.is_file() for path in trajectory_files)
    for path in trajectory_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 2
        assert payload["protocol"] == {"task_plan": 1, "answer_schema": 2}
        assert payload["provider"] == "scripted_offline_only"
        assert payload["responses"]
        assert all("tool_calls" in response or "content" in response for response in payload["responses"])
        assert not _has_forbidden_fixture_key(payload), path.name
        case = parse_case({
            "id": payload["case_id"],
            "category": "price_comparison" if "price" in payload["case_id"] else "recommendation",
            "turns": [{"role": "user", "content": payload["input"]}],
            "expect": payload["expected"],
            "trajectory": payload["responses"],
        })
        assert len(case.trajectory) == len(payload["responses"])


def test_single_goal_cannot_bypass_the_visible_plan():
    replay = _fixture("price_a4l_answer_invalid.json")
    state = AgentState.create(replay["input"]["user_text"], "system")
    state.task_intent = normalize_task_intent(
        [replay["input"]["goal"]],
        requires_plan=replay["input"]["requires_plan"],
    )

    decision = preflight_tool_call(
        {
            "id": "fixture-price-call",
            "function": {
                "name": "price_compare",
                "arguments": {"series": "奥迪A4L", "year": 2022},
            },
        },
        state,
    )

    assert state.task_intent["needs_plan"] is True
    assert isinstance(decision, Reject), "单目标声明后不应在尚无计划时运行数据工具"


def test_finalize_can_run_as_the_last_open_plan_step(monkeypatch):
    replay = _fixture("price_a4l_answer_invalid.json")
    state = AgentState.create(replay["input"]["user_text"], "system")
    state.task_intent = normalize_task_intent([replay["input"]["goal"]], requires_plan=True)
    state.task_context = {"revision": 0}
    state.plan = [
        {
            "id": "query_price",
            "title": "查询符合条件的价格分布",
            "description": "查询已声明目标所需的价格统计",
            "tool_names": ["price_compare"],
            "goal_ids": ["g1"],
            "status": "complete",
            "kind": "data",
            "completion_criteria": "tool_result",
        },
        {
            "id": "finalize",
            "title": "整理并核验回答",
            "description": "验证并提交最终答案",
            "tool_names": ["finalize_answer"],
            "goal_ids": ["g1"],
            "status": "running",
            "kind": "finalize",
            "completion_criteria": "validated_answer",
        },
    ]
    state.tool_results = [
        {
            "tool_call_id": "fixture-price-call",
            "tool_name": "price_compare",
            "status": "done",
            "plan_step_id": "query_price",
            "goal_ids": ["g1"],
            "constraint_revision": 0,
            "output": {"ok": True},
        }
    ]
    from backend.data_loader import get_dataset_version
    state.evidence_records = [{
        "evidence_id": "fixture-price-median", "tool_call_id": "fixture-price-call",
        "metric": "price.median_yuan", "value": 209800, "unit": "CNY", "scope": "population",
        "sample_size": 261, "dataset_version": get_dataset_version(), "constraint_revision": 0,
        "goal_ids": ["g1"],
    }]

    # Isolate the plan gate from dataset loading; this test targets the
    # finalize-step state transition, not price calculation or answer syntax.
    import backend.agent_runtime.tool_policy as policy

    monkeypatch.setattr(policy, "validate_finalize_answer", lambda *_args: ValidationResult(True, [], "已核验答案"))
    decision = preflight_tool_call(
        {
            "id": "fixture-finalize-call",
            "function": {
                "name": "finalize_answer",
                "arguments": {
                    "schema_version": 2,
                    "result_kind": "data_answer",
                    "goal_ids": ["g1"],
                    "blocks": [
                        {"kind": "fact_ref", "evidence_id": "fixture-price-median", "format": "sentence"}
                    ],
                    "limitation_codes": ["listing_price_not_transaction"],
                },
            },
        },
        state,
    )

    assert not isinstance(decision, Reject), getattr(decision, "error_code", "")


def test_price_answer_allows_trusted_entities_and_ordinary_chinese_words():
    replay = _fixture("price_a4l_answer_invalid.json")

    result = validate_answer_draft(
        replay["repaired_draft"],
        replay["evidence"],
        dataset_version="fixture-dataset-v1",
        constraint_revision=0,
    )

    assert result.valid, result.errors
    assert "奥迪 A4L" in result.answer
    assert "低四分位" in result.answer
    assert "同一车系" in result.answer


def test_valid_claims_cannot_produce_an_empty_final_answer():
    replay = _fixture("price_a4l_answer_invalid.json")
    draft = {
        "claims": [replay["repaired_draft"]["claims"][0]],
        "limitations": [],
        "narrative": [],
    }

    result = validate_answer_draft(
        draft,
        replay["evidence"],
        dataset_version="fixture-dataset-v1",
        constraint_revision=0,
    )

    assert not result.valid, "有 claim 但没有任何可交付文字，不能判定答案成功"


def test_recommendation_compaction_keeps_candidate_names_bound_to_values():
    replay = _fixture("recommend_entity_loss.json")
    source = replay["tool_output"]
    evidence = evidence_from_tool_result(
        "recommend",
        source,
        "synthetic-recommend-call",
        constraint_revision=0,
        dataset_version="fixture-dataset-v1",
    )
    tool_output = {**source, "evidence": evidence}
    messages = [
        {"role": "system", "content": "仅根据证据作答"},
        {"role": "user", "content": replay["input"]["user_text"]},
        {
            "role": "tool",
            "name": "recommend",
            "tool_call_id": "synthetic-recommend-call",
            "content": json.dumps(tool_output, ensure_ascii=False),
        },
    ]
    budget = ContextBudget(
        max_context_tokens=20_000,
        reserved_output_tokens=1_000,
        safety_margin_tokens=100,
        min_recent_turns=2,
        max_single_tool_result_tokens=6_000,
    )

    window = build_context_window(
        messages,
        current_run_start=1,
        budget=budget,
        estimator=TokenEstimator(),
    )
    compacted_message = next(item for item in window.messages if item.get("role") == "tool")
    compacted = json.loads(compacted_message["content"])
    compacted_recommendations = {
        str(item.get("series")): item
        for item in compacted.get("recommendations", [])
        if isinstance(item, dict) and item.get("series")
    }

    expected_items = source["recommendations"]
    compacted_evidence = {
        str(item.get("evidence_id")): item
        for item in compacted.get("evidence", [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    expected_visible_items = expected_items[:len(compacted_recommendations)]
    assert [item.get("series") for item in compacted.get("recommendations", [])] == [
        item["series"] for item in expected_visible_items
    ]
    assert compacted["total_count"] == len(expected_items)
    assert compacted["visible_count"] == len(expected_visible_items)
    assert compacted["omitted_count"] == len(expected_items) - len(expected_visible_items)
    assert compacted["visible_count"] > 0
    for expected in expected_visible_items:
        visible = compacted_recommendations[expected["series"]]
        assert visible.get("entity_group_id") in compacted.get("entity_group_refs", [])
        bound_values = [compacted_evidence[ref] for ref in visible.get("evidence_refs", [])]
        median = next(item for item in bound_values if item["metric"].endswith(".median_wan"))
        size = next(item for item in bound_values if item["metric"].endswith(".sample_size"))
        assert median["value"] == expected["median_wan"]
        assert size["value"] == expected["sample_size"]
        assert all(item.get("entity_group_id") == visible["entity_group_id"] for item in bound_values)
