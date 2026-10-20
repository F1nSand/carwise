from __future__ import annotations
import threading
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ConfigDict, StrictStr
from backend.data_loader import get_dataset_snapshot
from backend.analysis.contracts import ANALYSIS_TYPES, AnalysisInputError
from backend.analysis.service import build_catalog, run_analysis, run_dashboard

class QueryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_type: StrictStr
    filters: dict = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)
    expected_dataset_version: StrictStr | None = None

class DashboardPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filters: dict = Field(default_factory=dict)
    expected_dataset_version: StrictStr | None = None

_GATE=threading.BoundedSemaphore(2)
def create_analysis_router(current_principal):
    router=APIRouter(prefix="/v1/analysis",tags=["analysis"])
    def enter():
        if not _GATE.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "analysis_busy",
                    "message": "分析服务正在处理其他请求，请稍后重试。",
                    "retryable": True,
                },
                headers={"Retry-After": "2"},
            )
    @router.get("/catalog")
    def catalog(_principal=Depends(current_principal)):
        try:return build_catalog(get_dataset_snapshot())
        except Exception as exc:raise HTTPException(status_code=503,detail={"code":"dataset_unavailable","message":str(exc)})
    @router.post("/query")
    def query(payload: QueryPayload, request: Request, _principal=Depends(current_principal)):
        enter()
        try:
            snap=get_dataset_snapshot()
            if payload.expected_dataset_version and payload.expected_dataset_version!=snap.version:raise HTTPException(status_code=409,detail={"code":"dataset_changed","dataset_version":snap.version})
            try:
                result=run_analysis(payload.analysis_type,payload.filters,payload.params,snap)
                result["request_id"]=getattr(getattr(request.state,"diagnostic",None),"get",lambda *_:None)("request_id") or request.headers.get("x-request-id")
                return result
            except AnalysisInputError as exc:raise HTTPException(status_code=422,detail={"code":exc.code,"message":str(exc)})
            except ValueError as exc:raise HTTPException(status_code=422,detail={"code":"invalid_params","message":str(exc)})
        finally:_GATE.release()
    @router.post("/dashboard")
    def dashboard(payload: DashboardPayload, request: Request, _principal=Depends(current_principal)):
        enter()
        try:
            snap=get_dataset_snapshot()
            if payload.expected_dataset_version and payload.expected_dataset_version!=snap.version:raise HTTPException(status_code=409,detail={"code":"dataset_changed","dataset_version":snap.version})
            try:
                result=run_dashboard(payload.filters,snap)
                result["request_id"]=getattr(getattr(request.state,"diagnostic",None),"get",lambda *_:None)("request_id") or request.headers.get("x-request-id")
                return result
            except AnalysisInputError as exc:raise HTTPException(status_code=422,detail={"code":exc.code,"message":str(exc)})
            except ValueError as exc:raise HTTPException(status_code=422,detail={"code":"invalid_filters","message":str(exc)})
        finally:_GATE.release()
    return router
