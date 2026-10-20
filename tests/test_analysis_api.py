import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient
import backend.analysis_api as api
class Snap:
    version="v-current"; catalog={}; all_series=[]
    dataframe=pd.DataFrame({"price_yuan":[10000],"mileage_km":[10],"production_year":[2020],"vehicle_age_years":[2],"brand_name":["A"],"series":["X"],"city_name":["a"],"energy_type":[None]})
def test_version_mismatch_and_unknown_params(monkeypatch):
 monkeypatch.setattr(api,"get_dataset_snapshot",lambda:Snap())
 app=FastAPI(); app.include_router(api.create_analysis_router(lambda:None)); client=TestClient(app)
 stale=client.post("/v1/analysis/query",json={"analysis_type":"price_distribution","expected_dataset_version":"old"})
 assert stale.status_code==409 and stale.json()["detail"]["code"]=="dataset_changed"
 bad=client.post("/v1/analysis/query",json={"analysis_type":"price_distribution","params":{"sql":"select 1"}})
 assert bad.status_code==422
 extra=client.post("/v1/analysis/query",json={"analysis_type":"price_distribution","file_path":"x"})
 assert extra.status_code==422


def test_request_id_and_busy_limit(monkeypatch):
 monkeypatch.setattr(api,"get_dataset_snapshot",lambda:Snap())
 app=FastAPI(); app.include_router(api.create_analysis_router(lambda:None)); client=TestClient(app)
 response=client.post("/v1/analysis/query",headers={"x-request-id":"req-123"},json={"analysis_type":"price_distribution"})
 assert response.status_code==200 and response.json()["request_id"]=="req-123"
 assert api._GATE.acquire(blocking=False)
 assert api._GATE.acquire(blocking=False)
 try:
  busy=client.post("/v1/analysis/dashboard",json={})
  assert busy.status_code==503 and busy.headers["retry-after"]=="2"
 finally:
  api._GATE.release()
  api._GATE.release()
