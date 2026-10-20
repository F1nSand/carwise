from backend.agent_runtime.answer_validation import validate_answer_draft, _unverified_number


EVIDENCE = {
    "evidence_id": "ev-price",
    "tool_call_id": "call-1",
    "dataset_version": "v2",
    "constraint_revision": 3,
    "metric": "price.median_yuan",
    "value": 123456.7,
    "unit": "CNY",
    "filters": ["车系 = 宝马3系", "城市 = 杭州"],
    "sample_size": 18,
    "scope": "population",
}


def test_historical_report_fact_is_explanation_only_and_keeps_version_label():
    historical = {
        "evidence_id": "hist:report-7:2:abcdef123456",
        "metric": "price.median_wan", "value": 12.3, "unit": "wan", "scope": "population",
        "sample_size": 10, "filters": ["budget_max_wan=15"], "dataset_version": "dataset-old",
        "source": "historical_report", "report_id": "report-7", "report_version": 2,
    }
    explanation = {
        "schema_version": 2, "result_kind": "explanation", "goal_ids": ["g1"],
        "blocks": [{"kind": "text", "text": "这是保存报告中的中位数"},
                   {"kind": "fact_ref", "evidence_id": historical["evidence_id"], "format": "sentence"}],
        "limitation_codes": [],
    }
    accepted = validate_answer_draft(
        explanation, [historical], dataset_version="dataset-new", constraint_revision=9,
        required_goal_ids=["g1"],
    )
    assert accepted.valid is True
    assert "report-7 v2" in accepted.answer
    assert "dataset-old" in accepted.answer
    assert "不是当前统计" in accepted.answer

    current_data_claim = {**explanation, "result_kind": "data_answer"}
    rejected = validate_answer_draft(
        current_data_claim, [historical], dataset_version="dataset-new", constraint_revision=9,
        required_goal_ids=["g1"],
    )
    assert rejected.valid is False
    assert any(issue["code"] == "stale_evidence" for issue in rejected.issues)


def _draft():
    return {
        "claims": [{
            "claim_id": "median", "evidence_id": "ev-price", "metric": "price.median_yuan",
            "value": 123457, "unit": "元", "label": "中位挂牌价", "scope": "population",
            "filters": ["车系 = 宝马3系", "城市 = 杭州"],
        }],
        "limitations": ["结果反映挂牌行情。"],
        "narrative": ["当前条件下的中位挂牌价为 {{claim:median}}"],
    }


def test_valid_rounding_is_rendered_from_evidence():
    result = validate_answer_draft(_draft(), [EVIDENCE], dataset_version="v2", constraint_revision=3)
    assert result.valid
    assert "挂牌价中位数：123457元（总体统计，样本量 18" in result.answer
    assert "123457" not in " ".join(_draft()["narrative"])


def test_numeric_claim_id_does_not_count_as_free_floating_number():
    draft = _draft()
    draft["claims"][0]["claim_id"] = "c1"
    draft["narrative"] = ["中位挂牌价：{{claim:c1}}"]

    result = validate_answer_draft(draft, [EVIDENCE], dataset_version="v2", constraint_revision=3)

    assert result.valid
    assert "123457元" in result.answer


def test_data_cleaning_filter_is_not_confused_with_user_query_condition():
    evidence = {
        **EVIDENCE,
        "filters": ["价格在 (0, 5000000) 元区间内", "车系 = 宝马3系"],
    }
    draft = _draft()
    draft["claims"][0]["filters"] = evidence["filters"]

    result = validate_answer_draft(draft, [evidence], dataset_version="v2", constraint_revision=3)

    assert result.valid
    assert "价格在 (0, 5000000) 元区间内" in result.answer


def test_tool_rendered_budget_range_and_sample_threshold_match_user_conditions():
    evidence = {
        **EVIDENCE,
        "filters": [
            "价格在 (0, 5000000) 元区间内",
            "挂牌价在 0.0~15.00 万元之间",
            "能源类型 = 纯电动",
            "仅纳入样本量 >= 15 台的车系",
        ],
    }
    draft = _draft()
    draft["claims"][0]["filters"] = ["挂牌价在 0~15 万元之间", "能源类型=纯电动"]

    result = validate_answer_draft(draft, [evidence])

    assert result.valid
    assert "仅纳入样本量 >= 15 台的车系" in result.answer


def test_rejects_budget_range_that_differs_from_evidence():
    evidence = {**EVIDENCE, "filters": ["挂牌价在 0~15 万元之间"]}
    draft = _draft()
    draft["claims"][0]["filters"] = ["挂牌价在 0~10 万元之间"]

    result = validate_answer_draft(draft, [evidence])

    assert not result.valid
    assert any("条件与证据不匹配" in error for error in result.errors)


def test_rejects_wrong_value_unit_condition_and_old_data_or_constraints():
    draft = _draft()
    draft["claims"][0]["value"] = 123456
    draft["claims"][0]["unit"] = "年"
    draft["claims"][0]["filters"] = ["车系=宝马3系", "年份=2022"]
    result = validate_answer_draft(draft, [EVIDENCE], dataset_version="v1", constraint_revision=4)
    assert not result.valid
    assert any("单位" in error for error in result.errors)
    assert any("数值" in error for error in result.errors)
    assert any("条件" in error for error in result.errors)
    assert any("旧数据版本" in error for error in result.errors)
    assert any("旧条件版本" in error for error in result.errors)


def test_rejects_sample_as_population_and_free_floating_numeric_text():
    sample = {**EVIDENCE, "scope": "representative_sample"}
    draft = _draft()
    draft["narrative"].append("总体共有 18 条结果")
    result = validate_answer_draft(draft, [sample])
    assert not result.valid
    assert any("代表性样本不能作为总体统计" in error for error in result.errors)
    assert any("未绑定的数字" in error for error in result.errors)


def test_v2_distinguishes_stale_evidence_from_an_unknown_reference():
    draft = {
        "schema_version": 2,
        "result_kind": "data_answer",
        "goal_ids": ["price"],
        "blocks": [{"kind": "fact_ref", "evidence_id": "ev-price", "format": "sentence"}],
        "limitation_codes": [],
    }

    stale = validate_answer_draft(
        draft, [EVIDENCE], dataset_version="v2", constraint_revision=4,
        required_goal_ids=["price"],
    )
    unknown = validate_answer_draft(
        {**draft, "blocks": [{"kind": "fact_ref", "evidence_id": "ev-missing", "format": "sentence"}]},
        [EVIDENCE], dataset_version="v2", constraint_revision=3,
        required_goal_ids=["price"],
    )

    assert not stale.valid and any(item["code"] == "stale_evidence" for item in stale.issues or [])
    assert not unknown.valid and any(item["code"] == "unknown_evidence" for item in unknown.issues or [])


def test_rejects_unbound_or_unknown_claim_placeholder():
    draft = _draft()
    draft["narrative"] = ["参考 {{claim:missing}}"]
    result = validate_answer_draft(draft, [EVIDENCE])
    assert not result.valid
    assert any("未验证的 claim" in error for error in result.errors)


def test_converts_yuan_to_wan_in_validated_output():
    draft = _draft()
    draft["claims"][0]["unit"] = "万元"
    draft["claims"][0]["value"] = 12.35
    result = validate_answer_draft(draft, [EVIDENCE])
    assert result.valid
    assert "12.35万元" in result.answer


def test_rejects_numeric_limitations_to_prevent_sidecar_leakage():
    draft = _draft()
    draft["limitations"] = ["共 18 条"]
    result = validate_answer_draft(draft, [EVIDENCE])
    assert not result.valid


def test_numeric_scanner_ignores_identifiers_quartile_labels_and_ordinary_chinese():
    for text in ("奥迪 A4L", "同一车系", "点名", "进一步", "25/75 分位"):
        assert _unverified_number(text) is None


def test_numeric_scanner_rejects_unbound_currency_year_and_vehicle_count():
    for text in ("价格为 198000 元", "2022 年车源", "三款车型"):
        assert _unverified_number(text) is not None


def test_v2_renders_entity_year_and_price_from_trusted_evidence():
    evidence = {
        **EVIDENCE,
        "evidence_id": "ev-a4l-median",
        "metric": "price.median_yuan",
        "value": 209800,
        "entity_label": "奥迪 A4L",
        "filters": ["车系 = 奥迪 A4L", "年份 = 2022"],
        "goal_ids": ["g1"],
    }
    draft = {
        "schema_version": 2,
        "result_kind": "data_answer",
        "goal_ids": ["g1"],
        "blocks": [
            {"kind": "text", "text": "您问的奥迪 A4L 价格如下。"},
            {"kind": "fact_ref", "evidence_id": "ev-a4l-median", "format": "sentence"},
        ],
        "limitation_codes": ["listing_price_not_transaction", "sample_scope"],
    }

    result = validate_answer_draft(
        draft, [evidence], dataset_version="v2", constraint_revision=3,
        required_goal_ids=["g1"],
    )

    assert result.valid, result.errors
    assert "奥迪 A4L：挂牌价中位数：209800元" in result.answer
    assert "年份 = 2022" in result.answer
    assert "不代表实际成交价" in result.answer


def test_v2_rejects_unbound_number_unknown_reference_and_missing_goal_evidence():
    draft = {
        "schema_version": 2,
        "result_kind": "data_answer",
        "goal_ids": ["g1"],
        "blocks": [
            {"kind": "text", "text": "约 15 万元即可买到。"},
            {"kind": "fact_ref", "evidence_id": "ev-missing", "format": "sentence"},
        ],
        "limitation_codes": [],
    }

    result = validate_answer_draft(draft, [], required_goal_ids=["g1"])

    assert not result.valid
    assert any(issue["code"] == "unbound_number" for issue in result.issues or [])
    assert any(issue["code"] == "unknown_evidence" for issue in result.issues or [])
    assert any(issue["code"] == "goal_without_evidence" for issue in result.issues or [])
    empty_draft = {**draft, "blocks": [{"kind": "fact_ref", "evidence_id": "ev-missing", "format": "sentence"}]}
    empty_result = validate_answer_draft(empty_draft, [], required_goal_ids=["g1"])
    assert any(issue["code"] == "empty_answer" for issue in empty_result.issues or [])


def test_v2_reference_issue_lists_current_expected_evidence_ids():
    evidence = {
        "evidence_id": "ev-current",
        "tool_call_id": "price-call",
        "dataset_version": "v2",
        "constraint_revision": 3,
        "metric": "price.median_yuan",
        "value": 200000,
        "unit": "CNY",
        "filters": ["车系 = 奥迪 A4L"],
        "sample_size": 20,
        "scope": "population",
        "goal_ids": ["g1"],
    }
    result = validate_answer_draft(
        {
            "schema_version": 2,
            "result_kind": "data_answer",
            "goal_ids": ["g1"],
            "blocks": [{"kind": "fact_ref", "evidence_id": "ev-unknown", "format": "sentence"}],
            "limitation_codes": [],
        },
        [evidence],
        dataset_version="v2",
        constraint_revision=3,
        required_goal_ids=["g1"],
    )

    issue = next(item for item in result.issues or [] if item["code"] == "unknown_evidence")
    assert issue["expected_refs"] == ["ev-current"]
