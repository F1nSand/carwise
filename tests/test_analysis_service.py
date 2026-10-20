import pandas as pd
from backend.analysis.service import run_dashboard, run_analysis
class Snap:
    version="v1"; catalog={}; all_series=[]
    dataframe=pd.DataFrame({"price_yuan":[10,20,30],"mileage_km":[1,2,3],"production_year":[2020]*3,"vehicle_age_years":[1,2,3],"brand_name":["A","B","A"],"series":["X","X","Y"],"city_name":["a","b","a"],"energy_type":[None]*3})
def test_dashboard_uses_same_version_and_six_results():
 result=run_dashboard({"series_selectors":[{"brand_name":"A","series":"X"},{"brand_name":"B","series":"absent"}]},Snap())
 assert result["dataset_version"]=="v1" and len(result["analyses"])==6
 assert result["overview"]["scope_rows"]==1
 assert [x["n"] for x in result["analyses"]["model_comparison"]["data"]["groups"]]==[1,0]
def test_params_are_strict():
 try:run_analysis("price_distribution",params={"bins":5.2},snapshot=Snap())
 except ValueError:pass
 else:raise AssertionError("float bins must fail")
 try:run_analysis("price_distribution",params={"path":"x"},snapshot=Snap())
 except ValueError:pass
 else:raise AssertionError("unknown params must fail")




def test_params_reject_falsey_non_objects():
 for value in ([], 0, ""):
  try:
   run_analysis("price_distribution",params=value,snapshot=Snap())
  except ValueError:
   pass
  else:
   raise AssertionError(f"params must reject {value!r}")
 assert run_analysis("price_distribution",params=None,snapshot=Snap())["status"]=="success"
