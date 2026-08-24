"""数据分析与报告导出：/api/analysis（ADR-0001 已弃用）、/api/report/export。"""
import os
import traceback

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse

from api import deps
from api.auth import require_auth
from api.serialization import _normalize_paths, _sanitize_result
from utils.logger_handler import logger

router = APIRouter()

_EXPORT_FORMATS = {"md", "docx", "pdf", "html"}


@router.post("/api/analysis")
async def api_analysis(request: Request, user=Depends(require_auth)):
    """数据分析：同步返回 JSON（带记忆管理）。"""
    body = await request.json()
    query = body.get("query", "").strip()
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

    user_id = user["user_id"]
    session_id = body.get("session_id", "").strip()
    try:
        turn = deps._get_memory_service(user_id).begin_turn(user_id, session_id, query)
        session_id = turn.session_id
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    try:
        analyst = deps._get_planner_agent(user_id)
        result = analyst.run({"query": query, "user_id": user_id})

        # 将分析结果摘要存入记忆
        report = result.get("report", {})
        summary_text = report.get("markdown", str(result.get("title", "")))
        if summary_text:
            deps._get_memory_service(user_id).end_turn(
                user_id, session_id, query, f"[分析结果] {summary_text[:500]}",
            )

        # 序列化时处理非 JSON 兼容类型 + 转换图表路径为 Web 可访问
        result = _sanitize_result(result)
        result = _normalize_paths(result)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Analysis error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "errors": [str(e)]},
            status_code=500,
        )


@router.post("/api/report/export")
async def api_export_report(request: Request, user=Depends(require_auth)):
    """导出报告为 Word/Markdown/PDF/HTML。

    入参 JSON: {"markdown": str, "title": str, "format": "md"|"docx"|"pdf"|"html"}
    返回 FileResponse 触发浏览器下载。
    """
    body = await request.json()
    markdown = body.get("markdown", "")
    title = body.get("title", "数据分析报告") or "数据分析报告"
    fmt = (body.get("format", "") or "").lower().strip()

    if not markdown.strip():
        return JSONResponse({"error": "No markdown content"}, status_code=400)
    if fmt not in _EXPORT_FORMATS:
        return JSONResponse({"error": f"Unsupported format: {fmt}"}, status_code=400)

    try:
        from agents.export_agent import ExportAgent
        # user_id 必传：Agent 的 LLM 按用户解析（网页设置 > .env），
        # 不传则钉死 .env 默认模型（免费额度耗尽时 403）
        result = ExportAgent(user_id=user["user_id"]).run({
            "markdown": markdown,
            "title": title,
            "formats": [fmt],
        })
        files = result.get("files", [])
        if not files:
            errs = result.get("errors", [])
            msg = errs[0] if errs else f"{fmt} export produced no file"
            return JSONResponse({"error": msg}, status_code=502)
        fpath = files[0]["path"]
        if not os.path.exists(fpath):
            return JSONResponse({"error": "Export file missing"}, status_code=502)
        # 浏览器下载文件名（FileResponse 的 filename 参数会自动生成
        # RFC 5987 filename*=UTF-8''... 头，正确处理中文等非 ASCII 文件名；
        # 不手写 Content-Disposition 以免 latin-1 编码失败）
        download_name = os.path.basename(fpath)
        return FileResponse(fpath, filename=download_name)
    except Exception as e:
        logger.error(f"Export error: {traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)
