import json

import pytest

from backend.agent_runtime.context_budget import (
    ContextBudget,
    ContextInputTooLarge,
    DEFAULT_CONTEXT_BUDGET,
    TokenEstimator,
    accumulate_usage,
    build_context_window,
)


def test_default_budget_separates_512k_input_and_64k_output_from_model_context():
    assert DEFAULT_CONTEXT_BUDGET.input_tokens == 524_288
    assert DEFAULT_CONTEXT_BUDGET.reserved_output_tokens == 65_536
    assert DEFAULT_CONTEXT_BUDGET.max_context_tokens >= (
        DEFAULT_CONTEXT_BUDGET.input_tokens
        + DEFAULT_CONTEXT_BUDGET.reserved_output_tokens
        + DEFAULT_CONTEXT_BUDGET.safety_margin_tokens
    )


def test_request_estimate_includes_tool_schemas():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "query"}]
    tools = [{
        "type": "function",
        "function": {
            "name": "long_tool_schema",
            "description": "A schema that contributes input tokens",
            "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
        },
    }]
    estimator = TokenEstimator()
    window = build_context_window(
        messages,
        current_run_start=1,
        budget=ContextBudget(10_000, 1_000, 100, 2, 500, max_input_tokens=8_000),
        estimator=estimator,
        tools=tools,
    )

    assert window.estimated_input_tokens > estimator.estimate_messages(messages)
    assert window.estimated_input_tokens >= estimator.estimate_tools(tools)


def test_context_window_is_a_noop_when_messages_fit():
    messages = [
        {"role": "system", "content": "固定约束"},
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "第一轮回答"},
        {"role": "user", "content": "当前问题"},
    ]
    window = build_context_window(
        messages,
        current_run_start=3,
        budget=ContextBudget(10_000, 1_000, 100, 2, 500),
        estimator=TokenEstimator(),
    )

    assert window.messages == messages
    assert window.omitted_message_count == 0
    assert window.compacted_tool_result_count == 0
    assert window.estimated_input_tokens > 0


def test_context_window_evicts_oldest_turns_and_keeps_system_current_and_two_recent():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old " * 100},
        {"role": "assistant", "content": "old answer " * 20},
        {"role": "user", "content": "recent one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "current question"},
    ]
    estimator = TokenEstimator()
    window = build_context_window(
        messages,
        current_run_start=5,
        budget=ContextBudget(estimator.estimate_messages(messages) - 250, 0, 0, 2, 10_000),
        estimator=estimator,
    )

    assert window.messages[0] == messages[0]
    assert all(item["content"] != "old " * 100 for item in window.messages)
    assert any(item.get("content") == "recent one" for item in window.messages)
    assert any(item.get("content") == "current question" for item in window.messages)
    assert window.omitted_message_count >= 2


def test_context_eviction_keeps_assistant_tool_call_and_tool_reply_atomic():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "old-call", "type": "function", "function": {"name": "recommend", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "old-call", "name": "recommend", "content": '{"ok":true}'},
        {"role": "assistant", "content": "old done"},
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
        {"role": "user", "content": "current question"},
    ]
    estimator = TokenEstimator()
    window = build_context_window(
        messages,
        current_run_start=7,
        budget=ContextBudget(estimator.estimate_messages(messages) - 180, 0, 0, 2, 10_000),
        estimator=estimator,
    )

    assert all(item.get("tool_call_id") != "old-call" for item in window.messages)
    assert all(
        not any(call.get("id") == "old-call" for call in item.get("tool_calls", []))
        for item in window.messages
    )
    assert all(item.get("content") != "old question" for item in window.messages)


def test_large_tool_result_is_compacted_without_a_model_call():
    raw_rows = [{"car_name": f"车源 {index}", "price_yuan": index * 1000} for index in range(200)]
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "查车源"},
        {
            "role": "tool",
            "tool_call_id": "csv-call",
            "name": "csv_query",
            "content": json.dumps({"ok": True, "match_count": 200, "median_wan": 12.5, "rows": raw_rows}),
        },
    ]
    window = build_context_window(
        messages,
        current_run_start=1,
        budget=ContextBudget(20_000, 1_000, 100, 2, 200),
        estimator=TokenEstimator(),
    )

    tool_message = next(item for item in window.messages if item["role"] == "tool")
    compacted = json.loads(tool_message["content"])
    assert window.compacted_tool_result_count == 1
    assert compacted["tool_name"] == "csv_query"
    assert compacted["sample_count"] == 200
    assert compacted["statistics"]["median_wan"] == 12.5
    assert "rows" not in compacted
    assert len(tool_message["content"]) < 200


def test_compacted_tool_result_preserves_claimable_evidence():
    evidence = {
        "evidence_id": "ev-price-median",
        "tool_call_id": "price-call",
        "dataset_version": "dataset-v2",
        "constraint_revision": 3,
        "metric": "price.median_yuan",
        "value": 162800,
        "unit": "CNY",
        "filters": ["车系 = 宝马3系"],
        "sample_size": 54,
        "scope": "population",
    }
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "比较宝马3系挂牌价"},
        {
            "role": "tool",
            "tool_call_id": "price-call",
            "name": "price_compare",
            "content": json.dumps({
                "ok": True,
                "median_yuan": 162800,
                "sample_size": 54,
                "rows": [{"large": "payload" * 300}],
                "evidence": [evidence],
            }),
        },
    ]

    window = build_context_window(
        messages,
        current_run_start=1,
        budget=ContextBudget(20_000, 1_000, 100, 2, 600),
        estimator=TokenEstimator(),
    )

    compacted = json.loads(next(item for item in window.messages if item["role"] == "tool")["content"])
    assert compacted["evidence"] == [{
        "evidence_id": "ev-price-median",
        "metric": "price.median_yuan",
        "value": 162800,
        "unit": "CNY",
        "scope": "population",
        "sample_size": 54,
        "dataset_version": "dataset-v2",
        "constraint_revision": 3,
        "filters": ["车系 = 宝马3系"],
    }]

    minimal_window = build_context_window(
        messages,
        current_run_start=1,
        budget=ContextBudget(20_000, 1_000, 100, 2, 350),
        estimator=TokenEstimator(),
    )
    minimal = json.loads(next(item for item in minimal_window.messages if item["role"] == "tool")["content"])
    assert minimal["evidence"][0]["evidence_id"] == "ev-price-median"
    assert minimal["evidence"][0]["metric"] == "price.median_yuan"
    assert minimal["evidence"][0]["value"] == 162800
    assert minimal["evidence"][0]["unit"] == "CNY"
    assert TokenEstimator().estimate_messages([next(item for item in minimal_window.messages if item["role"] == "tool")]) <= 350


def test_recommendation_compaction_keeps_complete_ranked_entity_groups_and_shared_filters():
    from backend.agent_runtime.evidence import evidence_from_tool_result
    from backend.agent_runtime.tool_contracts import normalize_tool_result

    raw = {
        "ok": True,
        "result_status": "success",
        "budget_count": 600,
        "n_candidates": 50,
        "filters_applied": ["挂牌价在 0~15 万元之间", "能源类型 = 增程式", "车龄 <= 3 年"],
        "note": "预算15万元、共600条车源，排序前6个候选。" + ("large raw payload " * 100),
        "recommendations": [
            {
                "series": f"推荐车系 {index}",
                "brand": f"品牌 {index}",
                "sample_size": 100 - index,
                "median_wan": 10 + index,
                "p25_wan": 9 + index,
                "p75_wan": 11 + index,
                "median_year": 2024 - index,
                "median_mileage_wan_km": 2.5 + index,
                "score": round(0.99 - index * 0.01, 2),
            }
            for index in range(6)
        ],
        "rows": [{"raw": "large " * 200} for _ in range(200)],
    }
    normalized = normalize_tool_result("recommend", raw)
    evidence = evidence_from_tool_result("recommend", normalized, "recommend-call", dataset_version="v1")
    for record in evidence:
        record["goal_ids"] = ["g1"]
    # Match the current runtime's bounded tool-message evidence projection.
    normalized["evidence"] = evidence[:32]
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "推荐增程车型"},
        {
            "role": "tool",
            "tool_call_id": "recommend-call",
            "name": "recommend",
            "content": json.dumps(normalized, ensure_ascii=False),
        },
    ]

    window = build_context_window(
        messages,
        current_run_start=1,
        budget=ContextBudget(100_000, 1_000, 100, 2, 100_000),
        estimator=TokenEstimator(),
    )

    tool_message = next(item for item in window.messages if item["role"] == "tool")
    compacted = json.loads(tool_message["content"])
    visible = compacted["recommendations"]
    compact_evidence = compacted["evidence"]
    assert "data" not in compacted
    assert "note" not in compacted
    assert compacted["total_count"] == 6
    assert compacted["visible_count"] == len(visible) == 4
    assert compacted["omitted_count"] == 2
    assert compacted["entity_group_refs"] == [item["entity_group_id"] for item in visible]
    assert compacted["filters_applied"] == raw["filters_applied"]
    assert [item["rank"] for item in visible] == [1, 2, 3, 4]
    assert [item["series"] for item in visible] == [f"推荐车系 {index}" for index in range(4)]
    assert [item["brand"] for item in visible] == [f"品牌 {index}" for index in range(4)]
    assert all(len(item["evidence_refs"]) == 7 for item in visible)
    visible_groups = {item["entity_group_id"] for item in visible}
    assert {item["entity_group_id"] for item in compact_evidence} == visible_groups
    assert len(compact_evidence) == 28
    for item in visible:
        group = [record for record in compact_evidence if record["entity_group_id"] == item["entity_group_id"]]
        assert {record["evidence_id"] for record in group} == set(item["evidence_refs"])
        # The named group binds the evidence refs to series/brand/rank once;
        # duplicating those labels on every metric prevented several groups
        # from fitting in the model-visible result budget.
        assert all("entity_label" not in record and "brand" not in record and "rank" not in record for record in group)
        assert all("dataset_version" not in record and "constraint_revision" not in record for record in group)
        assert all(record["unit"] and isinstance(record["value"], (int, float)) for record in group)
        assert {record["metric"].rsplit(".", 1)[-1] for record in group} == {
            "median_wan", "p25_wan", "p75_wan", "median_year",
            "median_mileage_wan_km", "score", "sample_size",
        }


def test_context_window_rejects_oversized_current_user_message():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "问题" * 500}]

    with pytest.raises(ContextInputTooLarge, match="context_input_too_large"):
        build_context_window(
            messages,
            current_run_start=1,
            budget=ContextBudget(128, 32, 16, 2, 64),
            estimator=TokenEstimator(),
        )


def test_usage_accumulation_sums_provider_counters_and_tracks_last_call():
    aggregate = accumulate_usage(None, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "prompt_cache_hit_tokens": 2})
    aggregate = accumulate_usage(aggregate, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "prompt_cache_hit_tokens": 4})

    assert aggregate["prompt_tokens"] == 20
    assert aggregate["completion_tokens"] == 10
    assert aggregate["total_tokens"] == 30
    assert aggregate["model_calls"] == 2
    assert aggregate["cache_hit_calls"] == 2
    assert aggregate["provider_metrics"]["prompt_cache_hit_tokens"] == 6
    assert aggregate["last_model_usage"]["prompt_tokens"] == 10


def test_report_tool_context_compaction_keeps_version_dataset_and_valid_json():
    budget = ContextBudget(
        max_context_tokens=5000, reserved_output_tokens=500, safety_margin_tokens=0,
        min_recent_turns=1, max_single_tool_result_tokens=700,
    )
    payload = {
        "ok": True, "source": "historical_report", "report_id": "report-x", "version": 4,
        "origin_run_id": "run-x", "dataset_version": "dataset-2024",
        "context_text": "historical summary " * 80,
        "content": {"summaryTitle": "报告", "answer": "历史答案", "summaryText": "摘要",
                    "answerEvidence": {"claims": [
                        {"evidenceId": f"e-{index}", "metric": "price.median_wan", "value": index,
                         "unit": "wan", "datasetVersion": "dataset-2024"}
                        for index in range(50)
                    ]}},
        "evidence_refs": [f"e-{index}" for index in range(50)],
    }
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "解释这份报告"},
        {"role": "assistant", "tool_calls": [{"id": "read-1", "type": "function",
                                                   "function": {"name": "read_report", "arguments": "{}"}}]},
        {"role": "tool", "name": "read_report", "tool_call_id": "read-1",
         "content": json.dumps(payload, ensure_ascii=False)},
    ]
    result = build_context_window(
        messages, current_run_start=1, budget=budget, estimator=TokenEstimator(),
    )
    compacted = json.loads(result.messages[-1]["content"])
    assert compacted["source"] == "historical_report"
    assert compacted["report_id"] == "report-x"
    assert compacted["version"] == 4
    assert compacted["dataset_version"] == "dataset-2024"
    assert result.compacted_tool_result_count == 1
