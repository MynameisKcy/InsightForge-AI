"""任务中心：/api/tasks（#1 Task System，P1）。

跨会话恢复的 API 面：任务列表 / 单任务详情 / 续跑（resume）。
- owner 隔离：所有查询按 require_auth 的 user_id 过滤；不存在/越权统一 404，
  不泄露任务存在性。
- resume 走同步 JSON（同 /api/analysis 模式），不新增 SSE 帧，规避前端协议锁步。
- 结果序列化复用 api.serialization（NaN/Timestamp 清洗 + 图表路径转 Web URL）。
"""
import traceback

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api import deps
from api.auth import require_auth
from api.errors import error_response
from api.serialization import _normalize_paths, _sanitize_result
from utils.logger_handler import logger

router = APIRouter()


def _summary(rec) -> dict:
    """列表页摘要字段（不返回全量 plan/stage_results，省带宽）。"""
    total = len(rec.plan or [])
    return {
        "id": rec.id,
        "title": rec.title,
        "status": rec.status,
        "query": rec.query,
        "completed": len(rec.completed_steps or []),
        "total": total,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
    }


@router.get("/api/tasks")
async def list_tasks(request: Request, limit: int = 20, user=Depends(require_auth)):
    """当前用户的任务列表（created_at 降序）。"""
    from memory.task_store import list_tasks

    limit = max(1, min(int(limit), 100))
    recs = list_tasks(user["user_id"], limit=limit)
    return JSONResponse(content={"tasks": [_summary(r) for r in recs]})


@router.get("/api/tasks/{task_id}")
async def get_task(task_id: str, user=Depends(require_auth)):
    """单任务详情（含 plan / completed_steps / stage_results 摘要）。"""
    from memory.task_store import get_task as load_task

    rec = load_task(user["user_id"], task_id)
    if rec is None:
        return error_response("任务不存在或无权访问", 404)
    data = _sanitize_result(rec.to_dict())
    return JSONResponse(content=data)


@router.post("/api/tasks/{task_id}/resume")
async def resume_task(request: Request, task_id: str, user=Depends(require_auth)):
    """续跑任务：回灌已完成步骤，只执行剩余步骤（同步返回完整分析结果）。"""
    body = await request.json()
    session_id = (body.get("session_id") or "").strip()
    try:
        analyst = deps._get_planner_agent(user["user_id"])
        result = analyst.resume(task_id, user["user_id"], session_id)
        if not result.get("success", False):
            # 业务级失败（任务不存在/数据集漂移/执行降级）带 error 文案返回
            return JSONResponse(content=_sanitize_result(result))
        result = _sanitize_result(result)
        result = _normalize_paths(result)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Task resume error: {traceback.format_exc()}")
        return error_response(str(e), 500)
