import json

from backend.agent_runtime.context_budget import TokenEstimator, _compact_tool_content


def _ambiguous_output(candidate_count=3):
    candidates = [
        {"brand_name": "比亚迪", "series": f"汉DM-{index}"}
        for index in range(candidate_count)
    ]
    return {
        "ok": False,
        "tool": "region_gap",
        "result_status": "error",
        "reason_code": "ambiguous_series",
        "requested_series": "汉",
        "candidate_selectors": candidates,
        "repair_hint": "请通过 ask_user 让用户从真实车系候选中消歧，并保留已确认的城市和其他筛选条件。",
        "dataset_version": "snapshot-123",
        "normalized_filters": {
            "series_selectors": [{"brand_name": "比亚迪", "series": "汉"}],
            "cities": ["北京", "上海", "广州", "深圳"],
            "year_min": 2020,
            "year_max": 2023,
            "price_max_yuan": 200000,
        },
    }


def test_series_recovery_contract_survives_normal_tool_compaction():
    estimator = TokenEstimator()
    content = _compact_tool_content(
        {"name": "region_gap", "content": json.dumps(_ambiguous_output(), ensure_ascii=False)},
        estimator,
        12000,
    )
    compact = json.loads(content)

    assert "analysis_result" not in compact
    assert compact["reason_code"] == "ambiguous_series"
    assert compact["requested_series"] == "汉"
    assert compact["candidate_selectors"] == _ambiguous_output()["candidate_selectors"]
    assert compact["dataset_version"] == "snapshot-123"
    assert compact["normalized_filters"]["cities"] == ["北京", "上海", "广州", "深圳"]
    assert "ask_user" in compact["repair_hint"]


def test_small_budget_trims_candidates_with_explicit_omission_and_valid_json():
    estimator = TokenEstimator()
    content = _compact_tool_content(
        {"name": "region_gap", "content": json.dumps(_ambiguous_output(30), ensure_ascii=False)},
        estimator,
        1200,
    )
    compact = json.loads(content)

    assert estimator.estimate_messages([{"role": "tool", "content": content}]) <= 1200
    assert compact["candidate_selectors"]
    assert compact["omitted_candidate_count"] > 0
    assert compact["normalized_filters"]["cities"] == ["北京", "上海", "广州", "深圳"]


def test_unknown_series_keeps_repair_metadata_without_fabricating_analysis():
    estimator = TokenEstimator()
    output = _ambiguous_output(0)
    output.update({"reason_code": "unknown_series", "candidate_selectors": [],
                   "requested_series": "不存在的车系"})
    content = _compact_tool_content(
        {"name": "region_gap", "content": json.dumps(output, ensure_ascii=False)},
        estimator,
        12000,
    )
    compact = json.loads(content)

    assert "analysis_result" not in compact
    assert compact["reason_code"] == "unknown_series"
    assert compact["requested_series"] == "不存在的车系"
    assert compact["candidate_selectors"] == []
    assert compact["normalized_filters"]["cities"] == ["北京", "上海", "广州", "深圳"]


def test_brand_city_recovery_is_compacted_for_non_region_tools_with_contract_and_filters():
    estimator = TokenEstimator()
    for tool_name, reason, field in [
        ("price_compare", "ambiguous_brand", "brand"),
        ("brand_profile", "unknown_city", "city"),
    ]:
        output = _ambiguous_output(0)
        output.update({"tool": tool_name, "reason_code": reason, "keyword_field": field,
                       "requested_keyword": "输入名称", "candidate_values": ["真实候选"] if reason.startswith("ambiguous") else [],
                       "candidate_selectors": [], "requested_series": None,
                       "normalized_filters": {"brand": "用户品牌", "cities": ["北京", "上海"],
                                              "budget_min_wan": 5, "year_min": 2020}})
        compact = json.loads(_compact_tool_content(
            {"name": tool_name, "content": json.dumps(output, ensure_ascii=False)}, estimator, 12000
        ))
        assert compact["reason_code"] == reason
        assert compact["keyword_field"] == field
        assert compact["requested_keyword"] == "输入名称"
        assert compact["candidate_values"] == (["真实候选"] if reason.startswith("ambiguous") else [])
        assert compact["normalized_filters"]["cities"] == ["北京", "上海"]
        assert compact["normalized_filters"]["year_min"] == 2020
        assert compact["dataset_version"] == "snapshot-123"
        assert "analysis_result" not in compact


def test_small_budget_preserves_legacy_top_level_and_normalized_city_filters():
    estimator = TokenEstimator()
    output = _ambiguous_output(30)
    output.update({"tool": "price_compare", "city": "北京市", "candidate_values": [f"候选名称-{i}" for i in range(30)],
                   "normalized_filters": {"city": "北京市", "cities": ["北京市", "上海市"],
                                          "year_min": 2020, "year_max": 2024,
                                          "budget_min_wan": 3, "budget_max_wan": 15,
                                          **{f"other_filter_{i}": "条件" * 30 for i in range(8)}}})
    content = _compact_tool_content(
        {"name": "price_compare", "content": json.dumps(output, ensure_ascii=False)}, estimator, 1500
    )
    compact = json.loads(content)

    assert estimator.estimate_messages([{"role": "tool", "content": content}]) <= 1500
    assert compact["city"] == "北京市"
    assert compact["normalized_filters"]["city"] == "北京市"
    assert compact["normalized_filters"]["cities"] == ["北京市", "上海市"]
    assert compact["omitted_candidate_count"] or compact["omitted_candidate_value_count"]
