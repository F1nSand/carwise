import json
from pathlib import Path

from backend.tools.price_compare import compare_same_model
from backend.tools.price_decay import price_decay_curve
from backend.tools.recommend import recommend_cars
from backend.tools.region_gap import region_price_gap
from backend.data_loader import get_dataset_snapshot

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "artifacts" / "analysis-validation" / "baseline.json"


def test_legacy_tool_results_match_complete_recorded_baseline():
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert get_dataset_snapshot().version == baseline["dataset_version"]
    outputs = baseline["outputs"]
    series = baseline["inputs"]["series"]
    actual = {
        "price_compare": compare_same_model(series),
        "region_gap": region_price_gap(
            series,
            min_city_n=outputs["region_gap"]["min_city_n"],
        ),
        "price_decay": price_decay_curve(
            series,
            min_year_n=outputs["price_decay"]["min_year_n"],
        ),
        "recommend": recommend_cars(
            **{
                key: outputs["recommend"][key]
                for key in (
                    "budget_max_wan",
                    "budget_min_wan",
                    "city",
                    "brand",
                    "series",
                    "energy_type",
                )
            }
        ),
    }
    assert actual == outputs


def test_region_gap_legacy_threshold_filter_is_reported_once():
    result = region_price_gap("宝马3系", min_city_n=99999)
    expected_label = "仅纳入样本量 >= 99999 台的城市"
    assert result["filters_applied"].count(expected_label) == 1
