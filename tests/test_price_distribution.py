import pandas as pd
from backend.analysis.context import build_context
from backend.analysis.distribution import price_distribution
class S: version="v"; dataframe=pd.DataFrame({"price_yuan":[10,10,10],"mileage_km":[1,1,1],"production_year":[2020]*3,"vehicle_age_years":[1]*3,"brand_name":["a"]*3,"series":["s"]*3,"city_name":["x"]*3}); catalog={}; all_series=[]
def test_constant_distribution_nonzero_bin_and_conservation():
 r=price_distribution(build_context({},S()),{"bins":5}); assert len(r["data"]["bins"])==1 and r["data"]["bins"][0]["count"]==3
