import pandas as pd
import pytest
from backend.analysis.context import build_context
from backend.analysis.contracts import AnalysisInputError, normalize_filters
from backend.analysis.filtering import apply_filters

class Snap:
    version="v1"
    dataframe=pd.DataFrame({"price_yuan":[10000,20,5000000],"mileage_km":[None,-1,3],"production_year":[None,2020,2025],"brand_name":["A","B","A"],"series":["X","X","Y"],"city_name":["a","b","c"],"vehicle_age_years":[None,2,3],"energy_type":[None,"纯电动","纯电动"]})
    catalog={}; all_series=[]

def test_filter_boundaries_and_missing_semantics():
    ctx=build_context({},Snap())
    assert ctx.frame.price_yuan.tolist()==[10000,20]
    assert pd.isna(ctx.frame.iloc[0].mileage_km)

def test_series_identity_and_conflict():
    f=normalize_filters({"series_selectors":[{"brand_name":"A","series":"X"}]})
    assert build_context(f,Snap()).frame.brand_name.tolist()==["A"]
    with pytest.raises(AnalysisInputError): normalize_filters({"mystery":1})
    with pytest.raises(AnalysisInputError): build_context({"brands":["B"],"series_selectors":[{"brand_name":"A","series":"X"}]},Snap())

def test_nonfinite_and_ranges_rejected():
    with pytest.raises(AnalysisInputError): normalize_filters({"price_min_yuan":float("nan")})
    with pytest.raises(AnalysisInputError): normalize_filters({"year_min":2022,"year_max":2021})


def test_empty_brand_selection_preserves_schema_and_accepts_numeric_range():
    frame = pd.DataFrame({"price_yuan": [10000], "brand_name": ["甲"], "series": ["X"]})
    selected, _ = apply_filters(frame, {"brands": ["不存在"], "price_min_yuan": 9000})
    assert selected.empty
    assert list(selected.columns) == list(frame.columns)


def test_energy_enum_is_validated_globally_before_brand_scope():
    frame = pd.DataFrame({"price_yuan": [10000, 10000], "brand_name": ["甲", "乙"],
                          "series": ["A", "B"], "energy_type": ["纯电动", "插电式混合动力"]})
    selected, _ = apply_filters(frame, {"brands": ["甲"], "energy_types": ["插混"]})
    assert selected.empty
    with pytest.raises(AnalysisInputError, match="Unsupported energy type"):
        apply_filters(frame, {"brands": ["甲"], "energy_types": ["氢能"]})
