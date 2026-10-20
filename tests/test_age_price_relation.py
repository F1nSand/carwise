import pandas as pd
from backend.analysis.context import build_context
from backend.analysis.age_price import age_price_relation
class S: version="v"; dataframe=pd.DataFrame({"price_yuan":[10000+i for i in range(12)],"mileage_km":list(range(12)),"production_year":[2020]*12,"vehicle_age_years":[0,0.5]+[1]*10,"brand_name":["A"]*12,"series":["X"]*12,"city_name":["x"]*12}); catalog={}; all_series=[]
def test_uses_stored_age_and_keeps_small_bins():
 d=age_price_relation(build_context({},S()))["data"]; assert d["n"]==12 and d["missing_age_n"]==0 and sum(x["n"] for x in d["bins"])==12
