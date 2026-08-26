"""分析结果的 JSON 序列化清洗与路径→Web URL 转换。"""

from utils.report_paths import fs_to_web_url


def _sanitize_result(result: dict) -> dict:
    """确保结果可 JSON 序列化。"""
    sanitized = {}
    for key, value in result.items():
        if key == "results":
            sanitized[key] = _sanitize_dict(value)
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_dict(value)
        elif isinstance(value, list):
            sanitized[key] = [_sanitize_dict(v) if isinstance(v, dict) else v for v in value]
        else:
            sanitized[key] = value
    return sanitized


def _json_safe(obj):
    """递归把 NaN/Infinity 转成 None（JSON null），pd.Timestamp 转 ISO 字符串。

    DuckDB 数值列含空单元格时，pandas to_dict 会产出 float('nan')，
    FastAPI JSONResponse（allow_nan=False）序列化会抛
    "Out of range float values are not JSON compliant"。此处统一清洗。
    日期列的 pd.Timestamp 同样无法被 JSONResponse 序列化（上传含日期列
    数据集报 500 的根因），一并转 ISO 字符串。
    """
    import math
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if type(obj).__name__ == "Timestamp":  # duck-typing，避免为单个类型引入 pandas
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _sanitize_dict(d: dict) -> dict:
    """递归清理字典中的非 JSON 类型。"""
    if not isinstance(d, dict):
        return d
    clean = {}
    for k, v in d.items():
        if isinstance(v, dict):
            clean[k] = _sanitize_dict(v)
        elif isinstance(v, list):
            clean[k] = [_sanitize_dict(i) if isinstance(i, dict) else i for i in v]
        elif isinstance(v, (str, int, float, bool, type(None))):
            clean[k] = v
        else:
            clean[k] = str(v)
    return clean


def _to_web_path(abs_path: str) -> str:
    """将绝对路径转为 Web 可访问的相对路径（约定属主 utils/report_paths.py）。"""
    return fs_to_web_url(abs_path)


def _normalize_paths(obj):
    """递归转换 dict/list 中的绝对路径为 Web URL。"""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            # 转换 path/url 字段
            if k in ("path", "file_path") and isinstance(v, str) and (":\\" in v or ":/" in v):
                result[k] = v  # 保留原始路径
                result["url"] = _to_web_path(v)  # 添加 Web 可访问 URL
            elif k == "charts" and isinstance(v, list):
                result[k] = [
                    {**c, "url": _to_web_path(c.get("path", ""))}
                    if isinstance(c, dict) and c.get("path") else c
                    for c in v
                ]
            elif isinstance(v, (dict, list)):
                result[k] = _normalize_paths(v)
            else:
                result[k] = v
        return result
    elif isinstance(obj, list):
        return [_normalize_paths(item) if isinstance(item, (dict, list)) else item for item in obj]
    return obj
