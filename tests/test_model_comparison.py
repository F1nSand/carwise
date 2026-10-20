import pandas as pd
from backend.analysis.context import build_context
from backend.analysis.comparison import model_comparison
class S: version="v"; dataframe=pd.DataFrame({"price_yuan":[10,20],"mileage_km":[1,2],"production_year":[2020]*2,"vehicle_age_years":[1,2],"brand_name":["A","B"],"series":["same","same"],"city_name":["x"]*2}); catalog={}; all_series=[]
def test_same_series_different_brand_kept_separate():
 f={"series_selectors":[{"brand_name":"A","series":"same"},{"brand_name":"B","series":"same"}]}; ctx=build_context({},S()); ctx.normalized_filters=f
 d=model_comparison(ctx)["data"]; assert [x["n"] for x in d["groups"]]==[1,1]
