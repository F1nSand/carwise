import json

import pandas as pd
import pytest

from backend.agent_runtime.context_budget import TokenEstimator, _compact_tool_content
from backend.agent_runtime.evidence import evidence_from_tool_result
from backend.agent_runtime.report_projection import build_report
from backend.agent_runtime.tool_registry import execute_registered_tool, get_tool
from backend.agent_runtime.tool_contracts import normalize_tool_result
from backend.agent_runtime.state import AgentState
from backend.agent_runtime.task_context import normalize_task_intent
from backend.agent_runtime.tool_policy import preflight_tool_call, Reject
from backend.data_loader import DatasetSnapshot
from backend.tools import shared_analysis


@pytest.mark.parametrize("series,filters,reason", [
    ("未知车系", {"cities": ["北京"]}, "unknown_series"),
    ("宝马3系", {"cities": ["未知城市"]}, "unknown_city"),
    ("宝马3系", {"brands": ["未知品牌"]}, "unknown_brand"),
])
def test_region_keyword_correction_preserves_legacy_year(monkeypatch, series, filters, reason):
    snapshot = DatasetSnapshot("year-fixture", pd.DataFrame({
        "brand_name": ["宝马"], "series": ["宝马3系"], "city_name": ["北京"],
    }), {}, [])
    monkeypatch.setattr(shared_analysis, "get_dataset_snapshot", lambda: snapshot)
    result = shared_analysis.shared_region_gap(series, year=2021, filters=filters)
    assert result["reason_code"] == reason
    assert result["normalized_filters"]["year_min"] == 2021
    assert result["normalized_filters"]["year_max"] == 2021


def _analysis_result(analysis_type="mileage_price_correlation"):
    return {
        "schema_version": "1.0", "analysis_type": analysis_type, "status": "success",
        "reason_code": None, "dataset_version": "sha256:actual-snapshot",
        "normalized_filters": {"series_selectors": [{"brand_name": "甲", "series": "同名车系"}]},
        "filters_applied": ["车系身份=甲/同名车系"],
        "counts": {"dataset_rows": 20, "scope_rows": 20, "valid_rows": 18, "excluded_rows": 2},
        "data": {"n": 18, "pearson_r": 0.7, "spearman_rho": 0.6,
                 "correlation_reason": None, "mileage_bins": [],
                 "scatter": {"total_points": 18, "display_points": 18, "sampled": False,
                             "points": [{"mileage_km": i, "price_yuan": i * 100} for i in range(18)]}},
        "limitations": ["相关不代表因果"], "methodology": {"pairwise": "finite"},
    }


def test_shared_tool_adapter_preserves_snapshot_bound_result(monkeypatch):
    result = _analysis_result()
    snapshot = DatasetSnapshot(version="fixture", dataframe=pd.DataFrame({"brand_name": ["甲"], "series": ["同名车系"]}), catalog={}, all_series=[])
    monkeypatch.setattr("backend.data_loader.get_dataset_snapshot", lambda: snapshot)
    monkeypatch.setattr(shared_analysis, "run_analysis", lambda *a, **kw: result)

    output = shared_analysis.mileage_price_correlation(
        {"series_selectors": [{"brand_name": "甲", "series": "同名车系"}]}, {},
    )

    assert output["ok"] is True
    assert output["dataset_version"] == "sha256:actual-snapshot"
    assert output["data"] == result


def test_shared_unsupported_status_survives_legacy_tool_normalization(monkeypatch):
    result = {**_analysis_result("model_comparison"), "status": "unsupported",
              "reason_code": "select_at_least_two_series"}
    monkeypatch.setattr(shared_analysis, "run_analysis", lambda *a, **kw: result)

    output = normalize_tool_result("model_comparison", shared_analysis.model_comparison())

    assert output["status"] == "unsupported"
    assert output["result_status"] == "unsupported"
    assert output["error_code"] == "unsupported_filter"


def test_registered_shared_tools_reject_arbitrary_sql_and_paths():
    names = {"price_distribution", "mileage_price_correlation", "model_comparison", "brand_profile", "age_price_relation"}
    assert all(get_tool(name) and get_tool(name).read_only for name in names)
    assert execute_registered_tool("price_distribution", {"path": "data.csv"})["ok"] is False
    assert execute_registered_tool("price_distribution", {"params": {"sql": "select *"}})["ok"] is False
    assert execute_registered_tool("price_distribution", {"filters": {"age": 3}})["ok"] is False


def test_shared_preflight_converts_confirmed_budget_to_yuan_and_rejects_dropped_filter():
    state = AgentState.create("查询预算内价格分布", "system")
    state.task_intent = normalize_task_intent([{"id": "g1", "description": "预算内分布", "deliverable": "结果"}])
    state.task_context = {"revision": 0, "constraints": {"budget_max_wan": {"value": 15, "status": "confirmed"}}}
    state.plan = [{"id": "data", "title": "分析", "description": "使用共享工具", "tool_names": ["price_distribution"],
                   "goal_ids": ["g1"], "kind": "data", "completion_criteria": "tool_result", "status": "running"},
                  {"id": "final", "title": "完成", "description": "回答", "tool_names": ["finalize_answer"],
                   "goal_ids": ["g1"], "kind": "finalize", "completion_criteria": "validated_answer", "status": "queued"}]
    allowed = preflight_tool_call({"id": "a", "function": {"name": "price_distribution", "arguments":
        {"filters": {"price_max_yuan": 150000}}}}, state)
    rejected = preflight_tool_call({"id": "b", "function": {"name": "price_distribution", "arguments": {}}}, state)

    assert not isinstance(allowed, Reject)
    assert isinstance(rejected, Reject) and rejected.error_code == "constraint_changed"


def test_region_legacy_series_resolves_joint_identity_on_same_snapshot(monkeypatch):
    frame = pd.DataFrame({"brand_name": ["甲", "乙"], "series": ["同名", "同名"]})
    snapshot = DatasetSnapshot(version="snapshot-1", dataframe=frame, catalog={}, all_series=[])
    captured = {}

    def fake_run(name, filters=None, params=None, snapshot=None):
        captured.update(name=name, filters=filters, params=params, snapshot=snapshot)
        return _analysis_result("region_gap")

    monkeypatch.setattr("backend.data_loader.get_dataset_snapshot", lambda: snapshot)
    monkeypatch.setattr(shared_analysis, "run_analysis", fake_run)

    shared_analysis.shared_region_gap("同名", year=2021, filters={"brands": ["甲"]})

    assert captured["name"] == "region_gap"
    assert captured["snapshot"] is snapshot
    assert captured["filters"]["series_selectors"] == [{"brand_name": "甲", "series": "同名"}]
    assert captured["filters"]["year_min"] == captured["filters"]["year_max"] == 2021


def test_region_legacy_call_keeps_old_handler_shape_and_unmatched_series_is_empty(monkeypatch):
    legacy = {"ok": True, "tool": "region_gap", "national_median_yuan": 100000, "cities": []}
    monkeypatch.setattr("backend.tools.region_gap.region_price_gap", lambda *a, **kw: legacy)
    assert shared_analysis.shared_region_gap("旧车系") is legacy

    frame = pd.DataFrame({"brand_name": ["甲"], "series": ["别的车系"]})
    snapshot = DatasetSnapshot(version="snapshot-empty", dataframe=frame, catalog={}, all_series=[])
    monkeypatch.setattr("backend.data_loader.get_dataset_snapshot", lambda: snapshot)
    result = shared_analysis.shared_region_gap("不存在", filters={})

    assert result["result_status"] == "error"
    assert result["reason_code"] == "unknown_series"
    assert result["candidate_selectors"] == []


def test_region_legacy_same_name_requires_brand_filter(monkeypatch):
    frame = pd.DataFrame({"brand_name": ["甲", "乙"], "series": ["同名", "同名"]})
    snapshot = DatasetSnapshot(version="region-ambiguous", dataframe=frame, catalog={}, all_series=[])
    monkeypatch.setattr("backend.data_loader.get_dataset_snapshot", lambda: snapshot)

    ambiguous = shared_analysis.shared_region_gap("同名", filters={})
    selected = shared_analysis.shared_region_gap("同名", filters={"brands": ["甲"]})

    assert ambiguous["reason_code"] == "ambiguous_series"
    assert set(ambiguous["available_brands"]) == {"甲", "乙"}
    assert selected["dataset_version"] == "region-ambiguous"


def test_agent_region_series_prefers_exact_identity_then_unique_brand_prefix(monkeypatch):
    frame = pd.DataFrame({"brand_name": ["宝马"] * 6, "series": ["宝马3系", "宝马3系GT"] * 3, "city_name": ["北京", "上海", "广州", "深圳", "北京", "上海"]})
    snapshot = DatasetSnapshot(version="region-prefix", dataframe=frame, catalog={}, all_series=[])
    captured = []

    def fake_run(name, filters=None, params=None, snapshot=None):
        captured.append((filters, params, snapshot))
        return _analysis_result("region_gap")

    monkeypatch.setattr("backend.data_loader.get_dataset_snapshot", lambda: snapshot)
    monkeypatch.setattr(shared_analysis, "run_analysis", fake_run)

    alias_result = shared_analysis.shared_region_gap("3系", year=2022, filters={
        "series_selectors": [{"brand_name": "宝马", "series": "3系"}],
        "cities": ["北京", "上海", "广州", "深圳"], "price_min_yuan": 80000,
    })
    exact_result = shared_analysis.shared_region_gap("宝马3系", filters={
        "series_selectors": [{"brand_name": "宝马", "series": "宝马3系"}],
        "cities": ["北京", "上海", "广州", "深圳"],
    })

    assert alias_result["result_status"] == exact_result["result_status"] == "success"
    assert captured[0][0]["series_selectors"] == [{"brand_name": "宝马", "series": "宝马3系"}]
    assert captured[1][0]["series_selectors"] == [{"brand_name": "宝马", "series": "宝马3系"}]
    assert captured[0][0]["cities"] == ["上海", "北京", "深圳", "广州"] or set(captured[0][0]["cities"]) == {"北京", "上海", "广州", "深圳"}
    assert captured[0][0]["price_min_yuan"] == 80000
    assert captured[0][0]["year_min"] == captured[0][0]["year_max"] == 2022
    assert all(item[2] is snapshot for item in captured)


def test_agent_region_han_alias_requires_user_disambiguation(monkeypatch):
    frame = pd.DataFrame({"brand_name": ["比亚迪"] * 7,
                          "series": ["汉DM", "汉DM-i", "汉EV", "汉DM", "汉DM-i", "汉EV", "汉DM"],
                          "city_name": ["北京", "上海", "广州", "深圳", "北京", "上海", "广州"]})
    snapshot = DatasetSnapshot(version="han-candidates", dataframe=frame, catalog={}, all_series=[])
    monkeypatch.setattr("backend.data_loader.get_dataset_snapshot", lambda: snapshot)
    monkeypatch.setattr(shared_analysis, "run_analysis", lambda *a, **kw: pytest.fail("ambiguous alias must not run analysis"))

    result = shared_analysis.shared_region_gap("汉", filters={
        "series_selectors": [{"brand_name": "比亚迪", "series": "汉"}],
        "cities": ["北京", "上海", "广州", "深圳"], "year_min": 2020, "year_max": 2023,
    })

    assert result["result_status"] == "error"
    assert result["reason_code"] == "ambiguous_series"
    assert result["candidate_selectors"] == [
        {"brand_name": "比亚迪", "series": "汉DM"},
        {"brand_name": "比亚迪", "series": "汉DM-i"},
        {"brand_name": "比亚迪", "series": "汉EV"},
    ]
    assert "ask_user" in result["repair_hint"]
    assert set(result["normalized_filters"]["cities"]) == {"北京", "上海", "广州", "深圳"}
    assert result["normalized_filters"]["year_min"] == 2020
    assert result["normalized_filters"]["year_max"] == 2023


def test_agent_region_unknown_series_never_falls_back_to_unfiltered_analysis(monkeypatch):
    snapshot = DatasetSnapshot(version="unknown-series", dataframe=pd.DataFrame({
        "brand_name": ["宝马", "奔驰", "宝马", "奔驰"], "series": ["宝马3系", "奔驰C级", "宝马3系", "奔驰C级"],
        "city_name": ["北京", "上海", "北京", "上海"],
    }), catalog={}, all_series=[])
    monkeypatch.setattr("backend.data_loader.get_dataset_snapshot", lambda: snapshot)
    monkeypatch.setattr(shared_analysis, "run_analysis", lambda *a, **kw: pytest.fail("unknown series must not run analysis"))

    result = shared_analysis.shared_region_gap("不存在的车系", filters={
        "series_selectors": [{"brand_name": "宝马", "series": "不存在的车系"}],
        "cities": ["北京", "上海"], "price_max_yuan": 200000,
    })

    assert result["result_status"] == "error"
    assert result["reason_code"] == "unknown_series"
    assert result["candidate_selectors"] == []
    assert result["normalized_filters"]["series_selectors"] == [{"brand_name": "宝马", "series": "不存在的车系"}]
    assert set(result["normalized_filters"]["cities"]) == {"北京", "上海"}
    assert result["normalized_filters"]["price_max_yuan"] == 200000


def test_agent_region_series_prefix_resolution_is_brand_scoped(monkeypatch):
    frame = pd.DataFrame({"brand_name": ["宝马", "奔驰", "宝马", "奔驰"], "series": ["宝马3系", "奔驰3系"] * 2,
                          "city_name": ["北京", "上海", "广州", "深圳"]})
    snapshot = DatasetSnapshot(version="brand-scope", dataframe=frame, catalog={}, all_series=[])
    captured = {}
    monkeypatch.setattr("backend.data_loader.get_dataset_snapshot", lambda: snapshot)
    monkeypatch.setattr(shared_analysis, "run_analysis", lambda name, filters=None, params=None, snapshot=None:
        captured.update(filters=filters, snapshot=snapshot) or _analysis_result("region_gap"))

    result = shared_analysis.shared_region_gap("3系", filters={
        "series_selectors": [{"brand_name": "奔驰", "series": "3系"}],
    })

    assert result["result_status"] == "success"
    assert captured["filters"]["series_selectors"] == [{"brand_name": "奔驰", "series": "奔驰3系"}]


def test_real_dataset_region_short_aliases_resolve_only_when_unique():
    from backend.data_loader import get_dataset_snapshot

    snapshot = get_dataset_snapshot()
    status, bmw, _ = shared_analysis._resolve_region_series(snapshot, "3系", ["宝马"])
    han_status, han, han_candidates = shared_analysis._resolve_region_series(snapshot, "汉", ["比亚迪"])

    assert status == "resolved"
    assert bmw == [{"brand_name": "宝马", "series": "宝马3系"}]
    assert han_status == "ambiguous" and han == []
    assert {item["series"] for item in han_candidates} >= {"汉DM", "汉DM-i", "汉EV"}


def test_region_legacy_keys_are_projected_additively_with_shared_analysis(monkeypatch):
    analysis = _analysis_result("region_gap")
    analysis["data"] = {"baseline_median_yuan": 100000, "baseline_n": 10, "baseline_label": "filtered_sample",
                        "valid_city_count": 1, "cities": [{"city": "杭州", "n": 10, "median_yuan": 110000,
                        "p25_yuan": 90000, "p75_yuan": 130000, "diff_yuan": 10000, "diff_pct": 10.0,
                        "eligible": True, "low_sample": False}]}
    monkeypatch.setattr(shared_analysis, "run_analysis", lambda *a, **kw: analysis)
    monkeypatch.setattr("backend.data_loader.get_dataset_snapshot", lambda: DatasetSnapshot(
        version="v1", dataframe=pd.DataFrame({"brand_name": ["甲"], "series": ["秦"]}), catalog={}, all_series=[]))
    result = shared_analysis.shared_region_gap("秦", filters={"series_selectors": [{"brand_name": "甲", "series": "秦"}]})
    assert result["analysis_type"] == "region_gap"
    assert result["national_median_yuan"] == 100000
    assert result["cities"][0]["median_yuan"] == 110000


def test_shared_analysis_evidence_uses_real_version_and_brand_series_identity():
    analysis = {
        "analysis_type": "model_comparison", "dataset_version": "sha256:real",
        "filters_applied": ["同名车系"], "counts": {"valid_rows": 25},
        "data": {"groups": [{"brand_name": "甲", "series": "同名", "n": 12,
                              "price": {"median_yuan": 120000, "p25_yuan": 100000, "p75_yuan": 140000},
                              "mileage": {"median_km": 30000}, "age": {"median_years": 4.0}}]},
    }
    output = {"ok": True, "result_status": "success", "analysis_type": "model_comparison",
              "dataset_version": "sha256:real", "filters_applied": ["同名车系"], "data": analysis}

    records = evidence_from_tool_result("model_comparison", output, "call-a", dataset_version="wrong-pre-call-version")
    median = next(row for row in records if row["metric"].endswith("price.median_yuan"))
    assert median["value"] == 120000
    assert median["dataset_version"] == "sha256:real"
    assert median["entity_label"] == "同名"
    assert median["brand"] == "甲"
    assert median["entity_group_id"].startswith("ent_")


def test_each_shared_analysis_evidence_path_resolves_trusted_numeric_value():
    examples = [
        ("price_distribution", {"n": 4, "median_yuan": 90000, "bins": []}, "data.data.median_yuan", 90000),
        ("mileage_price_correlation", {"n": 12, "pearson_r": -0.3, "spearman_rho": -0.2, "mileage_bins": []}, "data.data.pearson_r", -0.3),
        ("model_comparison", {"groups": [{"brand_name": "甲", "series": "A", "n": 11, "price": {"median_yuan": 100000}}]}, "data.data.groups.0.price.median_yuan", 100000),
        ("brand_profile", {"denominator_n": 12, "groups": [{"brand_name": "甲", "n": 12, "share": 1.0, "price": {"median": 100000}}]}, "data.data.groups.0.price.median", 100000),
        ("region_gap", {"cities": [{"city": "杭州", "n": 12, "median_yuan": 100000}]}, "data.data.cities.0.median_yuan", 100000),
        ("age_price_relation", {"n": 12, "correlation": {"pearson_r": -0.5}, "bins": []}, "data.data.correlation.pearson_r", -0.5),
    ]
    for analysis_type, data, expected_metric, expected_value in examples:
        analysis = {"analysis_type": analysis_type, "dataset_version": "version-a",
                    "counts": {"valid_rows": 12}, "data": data}
        output = {"ok": True, "result_status": "success", "analysis_type": analysis_type,
                  "dataset_version": "version-a", "data": analysis}
        records = evidence_from_tool_result(analysis_type, output, f"call-{analysis_type}")
        record = next(row for row in records if row["metric"] == expected_metric)
        assert record["value"] == expected_value
        assert record["dataset_version"] == "version-a"


def test_distribution_bin_evidence_uses_bin_count_and_fraction_unit():
    analysis = {"analysis_type": "price_distribution", "dataset_version": "v1",
                "counts": {"valid_rows": 10}, "data": {"n": 10,
                "bins": [{"count": 2, "share": 0.2, "lower_yuan": 0, "upper_yuan": 10}]}}
    records = evidence_from_tool_result("price_distribution", {"ok": True, "result_status": "success",
        "analysis_type": "price_distribution", "dataset_version": "v1", "data": analysis}, "bin-call")
    share = next(item for item in records if item["metric"] == "data.data.bins.0.share")
    count = next(item for item in records if item["metric"] == "data.data.bins.0.count")

    assert share["value"] == 0.2 and share["unit"] == "ratio"
    assert count["sample_size"] == share["sample_size"] == 2


def test_report_includes_shared_result_for_standalone_and_mixed_legacy_tools():
    analysis = _analysis_result()
    shared_output = {"ok": True, "result_status": "success", "analysis_type": analysis["analysis_type"],
                     "data": analysis}
    report = build_report(user_text="相关性", result={"summary": "结果", "tool_results": [
        {"tool_name": "mileage_price_correlation", "status": "done", "output": shared_output},
    ]})
    mixed = build_report(user_text="混合", result={"summary": "结果", "tool_results": [
        {"tool_name": "price_compare", "status": "done", "output": {"ok": True, "sample_size": 5,
          "price": {"median_yuan": 100000}, "filters_applied": []}},
        {"tool_name": "mileage_price_correlation", "status": "done", "output": shared_output},
    ]})

    assert report["analysisBlocks"] == [analysis]
    assert mixed["intent"] == "price_compare"
    assert mixed["analysisBlocks"] == [analysis]


def test_context_compaction_keeps_analysis_evidence_but_never_scatter_points():
    analysis = _analysis_result()
    evidence = {"evidence_id": "ev-1", "metric": "data.data.pearson_r", "value": 0.7,
                "unit": "correlation", "scope": "population", "sample_size": 18,
                "dataset_version": "sha256:actual-snapshot", "constraint_revision": 4,
                "entity_group_id": "ent-1", "entity_label": "同名", "brand": "甲"}
    output = {"ok": True, "result_status": "success", "analysis_type": analysis["analysis_type"],
              "dataset_version": analysis["dataset_version"], "filters_applied": analysis["filters_applied"],
              "data": analysis, "evidence": [evidence]}
    compact = json.loads(_compact_tool_content({"name": "mileage_price_correlation", "content": json.dumps(output)}, TokenEstimator(), 12000))

    encoded = json.dumps(compact, ensure_ascii=False)
    assert "analysis_result" in compact
    assert '"points": [' not in encoded and '"points":[' not in encoded
    assert '"mileage_km"' not in encoded
    assert "ev-1" in encoded
    assert "sha256:actual-snapshot" in encoded
    assert "entity_group_id" in encoded
