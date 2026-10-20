from backend.agent_runtime.evidence import evidence_from_tool_result


def test_price_evidence_binds_call_conditions_version_and_population_scope():
    records = evidence_from_tool_result(
        "price_compare",
        {
            "ok": True,
            "result_status": "success",
            "filters_applied": ["车系 = 宝马3系", "城市 = 杭州"],
            "sample_size": 17,
            "price": {"median_yuan": 123456.7},
        },
        "call-1",
        constraint_revision=4,
        dataset_version="fixture-v1",
    )

    record = next(item for item in records if item["metric"] == "price.median_yuan")
    assert record["evidence_id"].startswith("ev_")
    assert record["tool_call_id"] == "call-1"
    assert record["dataset_version"] == "fixture-v1"
    assert record["constraint_revision"] == 4
    assert record["metric"] == "price.median_yuan"
    assert record["value"] == 123456.7
    assert record["unit"] == "CNY"
    assert record["filters"] == ["车系 = 宝马3系", "城市 = 杭州"]
    assert record["sample_size"] == 17
    assert record["scope"] == "population"


def test_csv_rows_are_sample_evidence_and_failed_outputs_are_not_evidence():
    records = evidence_from_tool_result(
        "csv_query",
        {"ok": True, "result_status": "success", "match_count": 50, "rows": [{"price_wan": 12.3}]},
        "call-2", dataset_version="fixture-v1",
    )
    row_price = next(item for item in records if item["metric"] == "rows.0.price_wan")
    assert row_price["scope"] == "representative_sample"
    assert evidence_from_tool_result("price_compare", {"ok": False, "result_status": "empty"}, "call-3", dataset_version="v") == []


def test_recommendation_metrics_share_a_stable_ranked_entity_group():
    output = {
        "ok": True,
        "result_status": "success",
        "filters_applied": ["挂牌价在 0~15 万元之间", "能源类型 = 增程式"],
        "recommendations": [
            {
                "series": "星越L",
                "brand": "吉利",
                "sample_size": 80,
                "median_wan": 12.8,
                "p25_wan": 11.4,
                "p75_wan": 14.1,
                "median_year": 2022,
                "median_mileage_wan_km": 3.6,
                "score": 0.91,
            },
        ],
    }

    records = evidence_from_tool_result("recommend", output, "recommend-call", dataset_version="v1")
    again = evidence_from_tool_result("recommend", output, "recommend-call", dataset_version="v1")
    changed_call = evidence_from_tool_result("recommend", output, "another-call", dataset_version="v1")

    assert len(records) == 7
    assert {record["entity_group_id"] for record in records} == {records[0]["entity_group_id"]}
    assert records[0]["entity_group_id"].startswith("ent_")
    assert [record["evidence_id"] for record in records] == [record["evidence_id"] for record in again]
    assert records[0]["entity_group_id"] != changed_call[0]["entity_group_id"]
    assert all(record["entity_label"] == "星越L" for record in records)
    assert all(record["brand"] == "吉利" and record["rank"] == 1 for record in records)
    assert all(record["goal_ids"] == [] for record in records)
    assert {record["metric"] for record in records} == {
        "recommendations.0.median_wan",
        "recommendations.0.p25_wan",
        "recommendations.0.p75_wan",
        "recommendations.0.median_year",
        "recommendations.0.median_mileage_wan_km",
        "recommendations.0.score",
        "recommendations.0.sample_size",
    }


def test_evidence_normalizer_accepts_canonical_data_payload():
    records = evidence_from_tool_result(
        "recommend",
        {
            "ok": True,
            "result_status": "success",
            "data": {
                "filters_applied": ["预算 <= 15 万元"],
                "recommendations": [{
                    "series": "秦PLUS",
                    "brand": "比亚迪",
                    "median_wan": 9.8,
                    "sample_size": 40,
                }],
            },
        },
        "canonical-call",
        dataset_version="v1",
    )

    median = next(item for item in records if item["metric"] == "recommendations.0.median_wan")
    assert median["value"] == 9.8
    assert median["entity_label"] == "秦PLUS"
    assert median["brand"] == "比亚迪"
    assert median["rank"] == 1
    assert median["filters"] == ["预算 <= 15 万元"]


def test_dataset_version_is_content_fingerprint_and_changes_with_file(tmp_path, monkeypatch):
    from backend import data_loader

    source = tmp_path / "data.parquet"
    source.write_bytes(b"fixture-a")
    monkeypatch.setattr(data_loader, "PARQUET_PATH", source)
    data_loader._dataset_version_for_stat.cache_clear()
    first = data_loader.get_dataset_version()
    source.write_bytes(b"fixture-b-different")
    second = data_loader.get_dataset_version()
    assert first != second
