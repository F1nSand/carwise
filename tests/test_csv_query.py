import inspect

import pandas as pd
import pytest

import backend.tools.csv_query as csv_query
from backend.data_loader import DatasetSnapshot


@pytest.fixture
def sample_df():
    return pd.DataFrame(
        [
            {
                "series": "宝马3系",
                "brand_name": "宝马",
                "city_name": "北京",
                "production_year": 2022,
                "price_yuan": 180000,
                "mileage_km": 30000,
                "car_name": "宝马3系 2022款",
                "registration_date": "2022-01-01",
                "data_quality_score": 100,
            },
            {
                "series": "奥迪A4L",
                "brand_name": "奥迪",
                "city_name": "上海",
                "production_year": 2021,
                "price_yuan": 160000,
                "mileage_km": 40000,
                "car_name": "奥迪A4L 2021款",
                "registration_date": "2021-02-01",
                "data_quality_score": 100,
            },
        ]
    )


def test_query_csv_dataset_returns_filtered_rows(monkeypatch, sample_df):
    monkeypatch.setattr(csv_query, "get_dataset_snapshot",
                        lambda: DatasetSnapshot("csv-v1", sample_df, {}, []))

    result = csv_query.query_csv_dataset(
        series="宝马3系",
        city="北京",
        min_price_wan=15,
        max_price_wan=20,
        limit=5,
    )

    assert result["ok"] is True
    assert result["tool"] == "csv_query"
    assert result["match_count"] == 1
    assert result["rows"][0]["series"] == "宝马3系"
    assert result["rows"][0]["price_wan"] == 18.0
    assert result["dataset_version"] == "csv-v1"


def test_query_csv_dataset_empty_result_is_structured(monkeypatch, sample_df):
    monkeypatch.setattr(csv_query, "get_dataset_snapshot",
                        lambda: DatasetSnapshot("csv-v1", sample_df, {}, []))

    result = csv_query.query_csv_dataset(series="不存在")

    assert result["ok"] is False
    assert result["result_status"] == "error"
    assert result["reason_code"] == "unknown_series"
    assert result["keyword_field"] == "series"
    assert result["requested_keyword"] == "不存在"


def test_query_csv_dataset_rejects_unsafe_or_unbounded_requests():
    signature = inspect.signature(csv_query.query_csv_dataset)
    assert "path" not in signature.parameters

    with pytest.raises(ValueError, match="limit"):
        csv_query.query_csv_dataset(limit=51)

    with pytest.raises(ValueError, match="min_price_wan"):
        csv_query.query_csv_dataset(min_price_wan=20, max_price_wan=10)
