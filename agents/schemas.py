"""极简声明式 schema 校验器（#5 结构校验层）。

设计取舍：
- 手写不引 pydantic：校验需求只有「类型 + 必填 + 枚举」，60 行内自洽，
  测试零额外依赖；错误信息带路径，可随重试反馈给 LLM 精确修正。
- 宽进严出策略在调用方：schema 管结构（本模块），语义管内容
  （PlannerAgent._sanitize_plan 去重/上限/依赖重映射），两层互补不可互替。
- extra keys 一律放行：LLM 常附加说明性字段，强删只会逼出更差的输出。

schema 形态（仅支持以下键）：
  {"type": "object", "required": [k, ...], "properties": {k: subschema}}
  {"type": "str" | "int" | "float" | "bool" | "list" | "int_list" | "any"}
  {"type": "enum", "values": [...]}

validate(data, schema) 返回错误列表；空列表 = 通过。
"""

from typing import Any

# Pipeline 已知 agent 白名单（与 planner_agent.AGENT_LABELS / _agent_map 同源）
KNOWN_AGENTS = (
    "sql_query",
    "trend_analysis",
    "product_analysis",
    "risk_analysis",
    "visualization",
    "report",
    "export",
)

PLAN_SCHEMA: dict = {
    "type": "object",
    "required": ["plan"],
    "properties": {
        "plan": {
            "type": "list",
            "items": {
                "type": "object",
                "required": ["agent"],
                "properties": {
                    # step 允许缺省：planner 侧有重编号守卫兜底
                    "step": {"type": "int"},
                    "agent": {"type": "enum", "values": KNOWN_AGENTS},
                    "task": {"type": "str"},
                    "depends_on": {"type": "int_list"},
                },
            },
        },
        "title": {"type": "str"},
        "reasoning": {"type": "str"},
    },
}


def _is_int(v: Any) -> bool:
    """int 且非 bool（Python 的 bool 是 int 子类，True 会伪装成 1）。"""
    return isinstance(v, int) and not isinstance(v, bool)


def validate(data: Any, schema: dict, path: str = "$") -> list[str]:
    """按 schema 校验数据，返回错误列表（空列表 = 通过）。"""
    stype = schema.get("type", "any")

    if stype == "any":
        return []

    if stype == "object":
        if not isinstance(data, dict):
            return [f"{path}: 应为 object，实际为 {type(data).__name__}"]
        errs: list[str] = []
        for key in schema.get("required", []):
            if key not in data:
                errs.append(f"{path}.{key}: 缺少必填字段")
        for key, sub in schema.get("properties", {}).items():
            if key in data:
                errs.extend(validate(data[key], sub, f"{path}.{key}"))
        return errs

    if stype == "list":
        if not isinstance(data, list):
            return [f"{path}: 应为 list，实际为 {type(data).__name__}"]
        items = schema.get("items")
        if not items:
            return []
        errs = []
        for i, item in enumerate(data):
            errs.extend(validate(item, items, f"{path}[{i}]"))
        return errs

    if stype == "str":
        if not isinstance(data, str):
            return [f"{path}: 应为 string，实际为 {type(data).__name__}"]
        return []

    if stype == "int":
        if not _is_int(data):
            return [f"{path}: 应为整数，实际为 {data!r}"]
        return []

    if stype == "float":
        # int 可无损当 float 用；bool 仍排除
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            return [f"{path}: 应为数值，实际为 {data!r}"]
        return []

    if stype == "bool":
        if not isinstance(data, bool):
            return [f"{path}: 应为布尔值，实际为 {data!r}"]
        return []

    if stype == "int_list":
        if not isinstance(data, list):
            return [f"{path}: 应为整数列表，实际为 {type(data).__name__}"]
        bad = [v for v in data if not _is_int(v)]
        if bad:
            return [f"{path}: 应为整数列表，发现非整数元素 {bad[:3]!r}"]
        return []

    if stype == "enum":
        values = schema.get("values", ())
        if data not in values:
            preview = list(values)[:5]
            return [f"{path}: 值 {data!r} 不在允许范围 {preview}{'...' if len(values) > 5 else ''} 内"]
        return []

    # 未知 type 视为 any（防御式：schema 写错不炸调用方）
    return []
