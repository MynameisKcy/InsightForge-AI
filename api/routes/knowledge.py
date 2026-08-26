"""知识库管理路由（方案C-5）：/api/knowledge/* + 统一文件列表 /api/files。"""
import os
import traceback

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse

from api import deps
from api.auth import require_auth
from api.errors import error_response
from utils.logger_handler import logger
from utils.path_tool import get_abs_path

router = APIRouter()


def _kb_data_dir(user_id: str | None = None) -> str:
    from utils.config_handler import chroma_conf
    base = get_abs_path(chroma_conf["data_path"])
    # 按用户分目录：data/<user_id>/，消除同名文件覆盖与磁盘级跨用户泄露
    if not user_id:
        return base
    return os.path.join(base, user_id)


def _kb_allowed_types() -> tuple:
    from utils.config_handler import chroma_conf
    return tuple(chroma_conf["allowed_knowledge_file_type"])


@router.get("/api/knowledge/files")
async def kb_list_files(request: Request, user=Depends(require_auth)):
    """列出 data/ 下知识库文件，含大小/类型/md5/是否已入库。"""
    user_id = user["user_id"]
    data_dir = _kb_data_dir(user_id)
    allowed = _kb_allowed_types()
    from utils.file_handler import get_file_md5_hex

    vs = deps._get_vector_store()
    vs._load_md5_store()
    # "已入库"以 chroma 实际数据为准（md5 仅去重提示，可能与 chroma 偏离）
    chroma_sources = vs.chroma_sources(user_id)
    chroma_basenames = {os.path.basename(s) for s in chroma_sources}
    files = []
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            fpath = os.path.join(data_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower().lstrip(".")
            if ext not in allowed:
                continue
            size = os.path.getsize(fpath)
            md5 = get_file_md5_hex(fpath) or ""
            files.append({
                "filename": fname,
                "size": size,
                "type": ext,
                "md5": md5,
                "ingested": (fpath in chroma_sources) or (fname in chroma_basenames),
            })
    return JSONResponse({"files": files, "count": len(files)})


@router.get("/api/files")
async def list_all_files(request: Request, user=Depends(require_auth)):
    """统一文件列表（需求②）：文本类（Chroma 知识库）+ 表格类（DuckDB 数据集）。"""
    user_id = user["user_id"]
    files = []
    # ── 文本类（PDF/Word/TXT/MD，进 Chroma）──
    data_dir = _kb_data_dir(user_id)
    allowed = _kb_allowed_types()
    from utils.file_handler import get_file_md5_hex
    try:
        vs = deps._get_vector_store()
        vs._load_md5_store()
        # "已入库"以 chroma 实际数据为准（md5 仅去重提示，可能与 chroma 偏离）
        chroma_sources = vs.chroma_sources(user_id)
        chroma_basenames = {os.path.basename(s) for s in chroma_sources}
    except Exception as e:
        logger.warning(f"加载向量库 md5 失败: {e}")
        chroma_sources = set()
        chroma_basenames = set()
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            fpath = os.path.join(data_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower().lstrip(".")
            if ext not in allowed:
                continue
            ingested = (fpath in chroma_sources) or (fname in chroma_basenames)
            status = "已完成" if ingested else "处理中"
            files.append({
                "name": fname, "type": "text",
                "size": os.path.getsize(fpath),
                "upload_time": "", "status": status, "source": "chroma",
            })
    # ── 表格类（CSV/Excel，进 DuckDB）──
    from database.datasources_db import datasources_db
    try:
        for d in datasources_db.list_datasets(owner_user_id=user_id):
            files.append({
                "name": d.get("name"), "type": "table",
                "size": d.get("row_count"),
                "upload_time": d.get("created_at", ""),
                "status": "已完成", "source": "duckdb",
                "table_name": d.get("table_name"),
            })
    except Exception as e:
        logger.warning(f"列数据集失败: {e}")
    return JSONResponse({"files": files, "count": len(files)})


@router.post("/api/knowledge/upload")
async def kb_upload(request: Request, files: list[UploadFile] = File(...), user=Depends(require_auth)):
    """上传文件到 data/ 并增量入库。"""
    user_id = user["user_id"]
    data_dir = _kb_data_dir(user_id)
    allowed = _kb_allowed_types()
    os.makedirs(data_dir, exist_ok=True)
    vs = deps._get_vector_store()

    # 大小上限与数据集上传共用（属主 dataset_service）
    from database.dataset_service import MAX_UPLOAD_SIZE

    results = []
    for f in files:
        fname = os.path.basename(f.filename or "")
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        if ext not in allowed:
            results.append({"filename": fname, "success": False,
                            "error": f"不支持的文件类型: {ext}"})
            continue
        # 大文件保护：超过 100MB 拒绝；PDF/Excel >50MB 给预估提示
        size = f.size if hasattr(f, "size") and f.size is not None else 0
        if size > MAX_UPLOAD_SIZE:
            results.append({"filename": fname, "success": False,
                            "error": "文件过大（超过 100MB 上限），请拆分或压缩后再上传"})
            continue
        advisory = ""
        if ext in ("pdf", "docx") and size > 50 * 1024 * 1024:
            mb = max(1, round(size / 1024 / 1024))
            advisory = f"文件较大（{mb}MB），解析入库预计较慢，请耐心等待"
        fpath = os.path.join(data_dir, fname)
        try:
            content = await f.read()
            with open(fpath, "wb") as out:
                out.write(content)
            chunks, skipped = vs.load_single_document(fpath, user_id)
            results.append({
                "filename": fname,
                "success": True,
                "chunks": chunks,
                "skipped": skipped,
                "advisory": advisory,
            })
        except Exception as e:
            logger.error(f"知识库上传入库失败 {fname}: {traceback.format_exc()}")
            results.append({"filename": fname, "success": False, "error": str(e)})
    return JSONResponse({"results": results})


@router.delete("/api/knowledge/files/{filename}")
async def kb_delete_file(request: Request, filename: str, user=Depends(require_auth)):
    """删除 data/ 下指定文件，并从向量库移除其分片。"""
    user_id = user["user_id"]
    # 防路径穿越
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        return error_response("非法文件名", 400)
    data_dir = _kb_data_dir(user_id)
    fpath = os.path.join(data_dir, filename)
    if not os.path.isfile(fpath):
        return error_response("文件不存在", 404)
    try:
        vs = deps._get_vector_store()
        removed = vs.delete_by_source(fpath, user_id)
        os.remove(fpath)
        return JSONResponse({"success": True, "removed_chunks": removed})
    except Exception as e:
        logger.error(f"知识库删除失败 {filename}: {traceback.format_exc()}")
        return error_response(str(e), 500)


@router.post("/api/knowledge/reindex")
async def kb_reindex(request: Request, user=Depends(require_auth)):
    """清空向量库并全量重建索引（二次确认通过 confirm=true 才执行）。"""
    user_id = user["user_id"]
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("confirm"):
        return error_response("需传 confirm=true 以确认全量重建", 400)
    try:
        vs = deps._get_vector_store()
        result = vs.reindex_all(user_id)
        return JSONResponse({"success": True, **result})
    except Exception as e:
        logger.error(f"知识库重建失败: {traceback.format_exc()}")
        return error_response(str(e), 500)


@router.get("/api/knowledge/stats")
async def kb_stats(request: Request, user=Depends(require_auth)):
    """返回知识库统计信息。"""
    user_id = user["user_id"]
    try:
        vs = deps._get_vector_store()
        return JSONResponse(vs.get_stats(user_id))
    except Exception as e:
        return error_response(str(e), 500)
