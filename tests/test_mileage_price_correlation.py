import pandas as pd
from backend.analysis.context import build_context
from backend.analysis.correlation import mileage_price_correlation
class S: version="v"; dataframe=pd.DataFrame({"price_yuan":[10000+i*100 for i in range(12)],"mileage_km":list(range(12)),"production_year":[2020]*12,"vehicle_age_years":[1]*12,"brand_name":["A"]*12,"series":["X"]*12,"city_name":["x"]*12}); catalog={}; all_series=[]
def test_perfect_positive_and_scatter():
 d=mileage_price_correlation(build_context({},S()))["data"]; assert d["pearson_r"]==1 and d["spearman_rho"]==1 and d["scatter"]["display_points"]==12
