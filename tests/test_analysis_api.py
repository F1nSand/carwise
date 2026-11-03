import pandas as pd
import pytest
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

@pytest.mark.parametrize("failure", [RuntimeError("secret=/srv/private/data.csv token=abc123"), FileNotFoundError("secret=/srv/private/data.csv token=abc123")])
@pytest.mark.parametrize("path, method, payload", [
    ("/v1/analysis/catalog", "get", None),
    ("/v1/analysis/query", "post", {"analysis_type": "price_distribution"}),
    ("/v1/analysis/dashboard", "post", {}),
])
def test_snapshot_failure_is_sanitized_retryable_and_releases_gate(monkeypatch, path, method, payload, failure, caplog):
    calls = 0
    def load_snapshot():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return Snap()

    monkeypatch.setattr(api, "get_dataset_snapshot", load_snapshot)
    app = FastAPI()
    app.include_router(api.create_analysis_router(lambda: None))
    client = TestClient(app)
    response = getattr(client, method)(path, json=payload) if payload is not None else client.get(path)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "2"
    assert response.json()["detail"] == {
        "code": "dataset_unavailable",
        "message": "数据集暂不可用，请稍后重试。",
        "retryable": True,
    }
    assert "/srv/private" not in response.text and "abc123" not in response.text
    assert str(failure) in caplog.text

    recovered = getattr(client, method)(path, json=payload) if payload is not None else client.get(path)
    assert recovered.status_code == 200
    acquired = 0
    try:
        assert api._GATE.acquire(blocking=False)
        acquired += 1
        assert api._GATE.acquire(blocking=False)
        acquired += 1
    finally:
        for _ in range(acquired):
            api._GATE.release()
