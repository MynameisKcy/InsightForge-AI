"""数据集管理路由：/api/datasets 列表/上传/删除/schema + /api/datasources/reload。

路由只做请求解析与 DatasetServiceError→HTTP 映射；数据集生命周期事务
（校验/落盘/DuckDB 装载/schema+sample 探测/元数据写入/失败补偿）属主在
database/dataset_service.py（架构评审 R2 候选6）。

datasources_db / init_duckdb 等保持函数级懒 import（重依赖晚加载，且是
tests monkeypatch 的属主模块接缝，见 test_datasources_reload_reindex_api）。
"""
import traceback

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse

from api.auth import require_auth
from api.errors import error_response
from database.dataset_service import DatasetServiceError, dataset_service
from utils.logger_handler import logger

router = APIRouter()


@router.get("/api/datasets")
async def list_datasets(request: Request, user=Depends(require_auth)):
    """列出所有可用数据集。"""
    from database.datasources_db import datasources_db
    datasets = datasources_db.list_datasets(owner_user_id=user["user_id"])
    return JSONResponse({"datasets": datasets, "count": len(datasets)})


@router.post("/api/datasets/upload")
async def upload_dataset(request: Request, file: UploadFile = File(...), user=Depends(require_auth)):
    """上传 CSV/Excel 文件，解析并加载到 DuckDB（事务在 DatasetService）。"""
    content = await file.read()
    try:
        payload = dataset_service.upload(content, file.filename or "", user["user_id"])
    except DatasetServiceError as e:
        return error_response(e.message, e.status_code)
    return JSONResponse(payload)


@router.delete("/api/datasets/{name}")
async def delete_dataset(request: Request, name: str, user=Depends(require_auth)):
    """删除数据集（卸载 DuckDB 表 + 删除文件 + 删除元数据）。"""
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        return error_response("非法数据集名", 400)
    try:
        dataset_service.delete(name, user["user_id"])
    except DatasetServiceError as e:
        return error_response(e.message, e.status_code)
    return JSONResponse({"success": True})


@router.get("/api/datasets/{name}/schema")
async def get_dataset_schema(request: Request, name: str, user=Depends(require_auth)):
    """获取数据集的详细 schema。"""
    try:
        body = dataset_service.schema(name, user["user_id"])
    except DatasetServiceError as e:
        return error_response(e.message, e.status_code)
    return JSONResponse(body)


@router.post("/api/datasources/reload")
async def reload_datasources(request: Request, user=Depends(require_auth)):
    """热加载 datasources.yml 配置的数据库连接。"""
    from database.duckdb_manager import init_duckdb

    try:
        db = init_duckdb(user_id=user["user_id"])
        result = db.register_external_databases()
        return JSONResponse({"success": True, **result})
    except Exception as e:
        logger.error(f"Datasource reload failed: {traceback.format_exc()}")
        return error_response(str(e), 500)
