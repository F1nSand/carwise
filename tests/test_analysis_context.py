import pandas as pd
from backend.analysis.context import build_context
from backend.analysis.service import run_analysis
class Snap:
    version="stable"; catalog={}; all_series=[]
    dataframe=pd.DataFrame({"price_yuan":[10000,20000],"mileage_km":[100,200],"production_year":[2020,2021],"vehicle_age_years":[1,2],"brand_name":["A","B"],"series":["X","Y"],"city_name":["a","b"],"energy_type":[None,None]})
def test_context_pins_single_snapshot_and_does_not_mutate():
    original=Snap.dataframe.copy(deep=True); ctx=build_context({},Snap())
    assert ctx.snapshot.version=="stable" and ctx.scope_rows==2
    assert Snap.dataframe.equals(original)
    result=run_analysis("price_distribution",snapshot=Snap())
    assert result["dataset_version"]=="stable"
