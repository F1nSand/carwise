import pandas as pd
from backend.analysis.context import build_context
from backend.analysis.brand_profile import brand_profile
class S: version="v"; dataframe=pd.DataFrame({"price_yuan":[10,20,30],"mileage_km":[1,2,3],"production_year":[2020]*3,"vehicle_age_years":[1]*3,"brand_name":["A","B",None],"series":["x","y","z"],"city_name":["x"]*3}); catalog={}; all_series=[]
def test_top_other_unknown_shares_conserve():
 d=brand_profile(build_context({},S()),{"top_k":1})["data"]; assert sum(x["n"] for x in d["groups"])==d["denominator_n"]==3
