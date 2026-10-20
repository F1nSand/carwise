from backend.agent_runtime.report_projection import build_report


def test_validated_answer_projection_keeps_legacy_shape_and_exposes_compact_evidence():
    result = {
        "summary": "unverified prose should not win",
        "summary_source": "agent",
        "final_message": {"content": "unverified prose should not win"},
        "answer_validation": {
            "valid": True,
            "answer": "中位挂牌价：12.3万元。",
            "claims": [{
                "claim_id": "median",
                "evidence": {
                    "evidence_id": "ev-one", "metric": "price.median_wan", "value": 12.3,
                    "unit": "wan", "filters": ["车系 = 示例"], "sample_size": 8,
                    "scope": "population", "dataset_version": "v1", "tool_call_id": "internal-call",
                    "constraint_revision": 4,
                },
            }],
            "limitations": ["价格是挂牌价。"],
        },
    }

    report = build_report(user_text="问题", result=result)

    assert report["answer"] == "中位挂牌价：12.3万元。"
    assert report["summaryText"] == report["answer"]
    assert report["answerEvidence"] == {
        "validated": True,
        "source": "agent_draft",
        "claims": [{
            "claimId": "median", "evidenceId": "ev-one", "metric": "price.median_wan",
            "value": 12.3, "unit": "wan", "filters": ["车系 = 示例"], "sampleSize": 8,
            "scope": "population", "datasetVersion": "v1",
        }],
        "limitations": ["价格是挂牌价。"],
    }
    assert "价格是挂牌价。" in report["limitations"]
    assert "tool_call_id" not in str(report["answerEvidence"])


def test_fallback_report_uses_exact_validated_answer_and_marks_source():
    result = {
        "summary": "validated answer",
        "summary_source": "validated",
        "answer_validation_fallback": True,
        "answer_validation": {"valid": True, "answer": "validated answer", "claims": [], "limitations": []},
    }
    report = build_report(user_text="问题", result=result)
    assert report["answer"] == report["summaryText"] == "validated answer"
    assert report["answerEvidence"]["source"] == "deterministic_fallback"


def test_recommend_result_is_projected_to_frontend_report_shape():
    report = build_report(
        user_text="15万以内帮我选3款车",
        result={
            "summary": "已完成推荐",
            "tool_results": [
                {
                    "tool_name": "recommend",
                    "output": {
                        "ok": True,
                        "tool": "recommend",
                        "budget_min_wan": 0,
                        "budget_max_wan": 15,
                        "filters_applied": ["挂牌价在 0~15 万元之间"],
                        "budget_count": 86008,
                        "n_candidates": 672,
                        "recommendations": [
                            {
                                "series": "宝马3系",
                                "brand": "宝马",
                                "sample_size": 1409,
                                "median_wan": 10.99,
                                "p25_wan": 8.78,
                                "p75_wan": 12.98,
                                "median_year": 2016,
                                "median_mileage_wan_km": 9.6,
                                "score": 0.779,
                            }
                        ],
                        "confidence": "high",
                        "weights": {"popularity": 0.5, "newness": 0.3, "low_mileage": 0.2},
                        "note": "挂牌价口径。",
                    },
                }
            ],
        },
    )

    assert report is not None
    assert report["id"]
    assert report["intent"] == "recommend"
    assert report["question"] == "15万以内帮我选3款车"
    assert report["budgetCount"] == 86008
    assert report["candidateCount"] == 672
    assert report["recommendations"][0]["series"] == "宝马3系"
    assert report["recommendations"][0]["reason"]
    assert report["recommendations"][0]["risk"]
    assert report["priceChart"] == [{"label": "宝马3系", "value": 10.99}]
    assert report["sampleChart"] == [{"label": "宝马3系", "value": 1409}]
    assert report["reliability"]["confidence"] == "high"


def test_price_result_projects_samples_and_optional_analysis_charts():
    report = build_report(
        user_text="宝马3系多少钱，给我3条代表性车源",
        result={
            "summary": "宝马3系中位挂牌价约 16.28 万。",
            "tool_results": [
                {
                    "tool_name": "price_compare",
                    "output": {
                        "ok": True,
                        "tool": "price_compare",
                        "series": "宝马3系",
                        "filters_applied": ["车系 = 宝马3系"],
                        "sample_size": 3089,
                        "price": {
                            "p25_wan": 11.58,
                            "median_wan": 16.28,
                            "p75_wan": 22.0,
                            "mean_wan": 16.75,
                            "min_yuan": 18000,
                            "max_yuan": 319000,
                        },
                        "dispersion": {"iqr_ratio": 0.64},
                        "confidence": "medium",
                        "representative_samples": [
                            {
                                "car_name": "宝马3系",
                                "year": 2019,
                                "city": "北京",
                                "price_yuan": 162800,
                                "mileage_km": 68000,
                            }
                        ],
                        "note": "价格为挂牌价。",
                    },
                },
                {
                    "tool_name": "price_decay",
                    "output": {
                        "ok": True,
                        "points": [
                            {"year": 2019, "median_wan": 16.28, "sample_size": 20},
                            {"year": 2020, "median_wan": 18.8, "sample_size": 25},
                        ],
                    },
                },
                {
                    "tool_name": "region_gap",
                    "output": {
                        "ok": True,
                        "cities": [
                            {"city": "北京", "median_wan": 16.5, "sample_size": 30},
                            {"city": "上海", "median_wan": 16.8, "sample_size": 28},
                        ],
                    },
                },
            ],
        },
    )

    assert report is not None
    assert report["intent"] == "price_compare"
    assert report["price"]["medianWan"] == 16.28
    assert report["price"]["iqrRatio"] == 0.64
    assert report["representativeListings"] == [
        {
            "carName": "宝马3系",
            "year": 2019,
            "city": "北京",
            "priceWan": 16.28,
            "mileageWanKm": 6.8,
        }
    ]
    assert report["yearChart"] == [
        {"label": "2019", "value": 16.28},
        {"label": "2020", "value": 18.8},
    ]
    assert report["cityChart"] == [
        {"label": "北京", "value": 16.5},
        {"label": "上海", "value": 16.8},
    ]


def test_direct_agent_answer_is_projected_without_inventing_data():
    report = build_report(
        user_text="二手车价格应该怎么看",
        result={
            "summary": "先看挂牌价和中位价。",
            "final_message": {"content": "先看挂牌价和中位价。"},
            "tool_results": [],
        },
    )

    assert report is not None
    assert report["intent"] == "unknown"
    assert report["scenarioLabel"] == "Agent 问答"
    assert report["summaryText"] == "先看挂牌价和中位价。"
    assert report["answer"] == "先看挂牌价和中位价。"
    assert report["reliability"]["confidence"] == "none"
    assert any("未调用结构化数据工具" in item for item in report["limitations"])


def test_runtime_failure_is_not_presented_as_a_successful_agent_answer():
    assert build_report(
        user_text="你能做什么",
        result={
            "summary": "当前模型不可用",
            "summary_source": "runtime",
            "final_message": {"content": "当前模型不可用"},
            "tool_results": [],
        },
    ) is None


def test_report_projection_marks_missing_derived_charts_as_limitations():
    report = build_report(
        user_text="宝马3系多少钱",
        result={
            "summary": "已完成价格比较",
            "tool_results": [
                {
                    "tool_name": "price_compare",
                    "output": {
                        "ok": True,
                        "series": "宝马3系",
                        "sample_size": 12,
                        "price": {
                            "p25_wan": 10,
                            "median_wan": 12,
                            "p75_wan": 14,
                            "mean_wan": 12,
                            "min_yuan": 100000,
                            "max_yuan": 140000,
                        },
                        "dispersion": {"iqr_ratio": 0.33},
                        "confidence": "low",
                        "representative_samples": [],
                        "filters_applied": [],
                    },
                }
            ],
        },
    )

    assert report is not None
    assert report["yearChart"] == []
    assert report["cityChart"] == []
    assert any("price_decay" in item for item in report["limitations"])
    assert any("region_gap" in item for item in report["limitations"])


def test_csv_query_only_result_is_projected_as_evidence_report():
    report = build_report(
        user_text="15万以内、3年内的 SUV 推荐几款",
        result={
            "summary": "已查询代表性车源样本。",
            "final_message": {"content": "已查询代表性车源样本。"},
            "tool_results": [
                {
                    "tool_name": "csv_query",
                    "status": "done",
                    "output": {
                        "ok": True,
                        "tool": "csv_query",
                        "filters_applied": ["挂牌价在 -~15 万元"],
                        "match_count": 68,
                        "rows": [{"series": "元PLUS", "price_wan": 11.38}],
                        "columns": ["series", "price_wan"],
                        "note": "共命中 68 条车源，展示前 1 条。",
                    },
                },
                {
                    "tool_name": "csv_query",
                    "status": "done",
                    "output": {
                        "ok": True,
                        "tool": "csv_query",
                        "filters_applied": ["车系 = 哈弗H6"],
                        "match_count": 19,
                        "rows": [{"series": "哈弗H6", "price_wan": 8.88}],
                        "columns": ["series", "price_wan"],
                        "note": "共命中 19 条车源，展示前 1 条。",
                    },
                },
            ],
        },
    )

    assert report is not None
    assert report["intent"] == "evidence"
    assert report["evidence"]["queryCount"] == 2
    assert [item["matchCount"] for item in report["evidence"]["queries"]] == [68, 19]
    assert report["answer"] == "已查询代表性车源样本。"
    assert any("代表性样本不等同于总体统计" in item for item in report["limitations"])


def test_empty_tool_result_is_retained_as_no_data_without_fake_zero_sample():
    report = build_report(
        user_text="查找一个不存在的车系",
        result={
            "summary": "没有形成可比样本。",
            "final_message": {"content": "没有形成可比样本。"},
            "tool_results": [
                {
                    "tool_name": "price_compare",
                    "status": "error",
                    "output": {
                        "ok": False,
                        "result_status": "empty",
                        "reason_code": "no_matching_samples",
                        "series": "不存在的车系",
                        "filters_applied": ["车系 = 不存在的车系"],
                        "observed": {"raw_count": 0},
                        "relaxation_candidates": [{"field": "city", "action": "remove"}],
                        "note": "未找到符合条件的可比车源。",
                    },
                }
            ],
        },
    )

    assert report is not None
    assert report["intent"] == "no_data"
    assert report["reliability"]["sampleSize"] is None
    assert report["reasonCode"] == "no_matching_samples"
    assert report["relaxationCandidates"][0]["field"] == "city"
