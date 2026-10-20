import pandas as pd
from backend.analysis.context import build_context
from backend.analysis.region import region_gap
class S: version="v"; dataframe=pd.DataFrame({"price_yuan":[10,20,30],"mileage_km":[1,2,3],"production_year":[2020]*3,"vehicle_age_years":[1]*3,"brand_name":["A"]*3,"series":["X"]*3,"city_name":["a","a","b"]}); catalog={}; all_series=[]
def test_baseline_is_before_city_threshold():
 d=region_gap(build_context({},S()),{"min_city_n":2})["data"]; assert d["baseline_median_yuan"]==20 and d["valid_city_count"]==1 and len(d["cities"])==2
