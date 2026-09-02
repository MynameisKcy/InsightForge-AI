"""Permission hooks（#4，P2）：集中式权限拦截总线。

背景：SQL 沙箱（database/safety.py）与文件路径检查散落在各调用点，新增工具
没有统一拦截口。本模块提供注册式 hook 总线，现有检查函数原样保留、作为
默认 hook 的委托实现（零重复，行为恒等），新增工具只需在对应拦截点
trigger 即可自动获得已有权限规则。

- 拦截点（point）：sql.execute / csv.load / file.write / tool.invoke。
- trigger_hooks(point, **payload) -> str | None：返回第一个拦截理由；None = 放行。
- 默认 hook 惰性注册（首次 trigger 时），可按需 register_hook 扩展；测试用
  clear_hooks() 重置。
- 语义：hook 内部异常按拒绝处理（fail-closed），异常消息即拦截理由；
  每次拦截落 decision_log（统一审计）。
"""

import threading

from utils.logger_handler import logger

POINT_SQL_EXECUTE = "sql.execute"
POINT_CSV_LOAD = "csv.load"
POINT_FILE_WRITE = "file.write"
POINT_TOOL_INVOKE = "tool.invoke"

_registry: dict[str, list] = {}
# 可重入锁：_ensure_defaults 持锁期间调 register_hook（同线程再取锁）不死锁
_lock = threading.RLock()
_defaults_ensured = False


def register_hook(point: str, fn) -> None:
    """注册 hook（同一 fn 重复注册去重）。fn(**payload) -> str|None。"""
    with _lock:
        hooks = _registry.setdefault(point, [])
        if fn not in hooks:
            hooks.append(fn)


def clear_hooks(point: str | None = None) -> None:
    """测试用：清空指定拦截点（None = 全部）并复位默认注册。"""
    global _defaults_ensured
    with _lock:
        if point is None:
            _registry.clear()
            _defaults_ensured = False
        else:
            _registry.pop(point, None)


def trigger_hooks(point: str, **payload) -> str | None:
    """触发拦截点，返回第一个拦截理由（None = 放行）。"""
    _ensure_defaults()
    with _lock:
        hooks = list(_registry.get(point, ()))
    for fn in hooks:
        try:
            reason = fn(**payload)
        except Exception as e:  # hook 异常 fail-closed：拒绝并留痕
            reason = f"permission hook 异常（{type(e).__name__}: {e}）"
        if reason:
            _audit(point, payload, reason)
            return str(reason)
    return None


def _audit(point: str, payload: dict, reason: str) -> None:
    try:
        from utils.decision_log import log_decision, make_decision
        log_decision(make_decision(
            source="permission",
            reasoning=f"拦截 {point}: {reason}",
            tool_selected=f"permission.{point}",
            tool_args={k: v for k, v in payload.items()
                       if not isinstance(v, (bytes, bytearray))},
        ))
    except Exception as e:
        logger.debug(f"permission audit failed: {e}")


# ── 默认 hook：委托现有 safety 函数，行为恒等 ──

def _hook_sql_execute(sql, user_id=None):
    """委托 database.safety.assert_read_only（sqlglot AST 只读白名单）。"""
    from database.safety import SecurityError, assert_read_only
    try:
        assert_read_only(sql)
        return None
    except SecurityError as e:
        return str(e)


def _hook_csv_load(path):
    """委托 database.safety.validate_csv_path（data 目录 + 引号防护）。"""
    from database.safety import SecurityError, validate_csv_path
    try:
        validate_csv_path(path)
        return None
    except SecurityError as e:
        return str(e)


def _allowed_write_roots() -> list[str]:
    """file.write 白名单根目录（reports / data 及其子目录，如 data/tasks、data/external）。"""
    import os

    from utils.path_tool import get_abs_path
    return [os.path.realpath(get_abs_path(p)) for p in ("reports", "data")]


def _hook_file_write(path, purpose=""):
    """文件写出白名单：realpath 必须落在 reports/data 之下（覆盖报告/图表/任务/数据集）。"""
    import os

    real = os.path.realpath(str(path or ""))
    for root in _allowed_write_roots():
        if real.startswith(root + os.sep) or real == root:
            return None
    return f"文件写出路径越界: {path!r}（仅允许 reports/ 与 data/ 目录）"


def _hook_tool_invoke(tool_name, args=None, user_id=None):
    """tool.invoke：P2 仅审计不设权限规则（返回 None 恒放行）；审计由 _audit 承担。"""
    return None


def _ensure_defaults() -> None:
    """惰性注册默认 hook（每个拦截点首次触发时生效）。"""
    global _defaults_ensured
    if _defaults_ensured:
        return
    with _lock:
        if _defaults_ensured:
            return
        register_hook(POINT_SQL_EXECUTE, _hook_sql_execute)
        register_hook(POINT_CSV_LOAD, _hook_csv_load)
        register_hook(POINT_FILE_WRITE, _hook_file_write)
        register_hook(POINT_TOOL_INVOKE, _hook_tool_invoke)
        _defaults_ensured = True
