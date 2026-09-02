"""Goal 独立判断器（#2，P2）：plan 完成 ≠ 用户目标达成。

在 PlannerAgent.run() 末尾对「用户原始 query（目标）+ 结果摘要」做一次独立
LLM 评估，输出 goal_check（goal_met / gap / suggested_followup），避免
「LLM 觉得做完了」掩盖「用户真正想要的没做」。

设计要点：
- fail-open：评估器自身异常 / 输出非法 schema → 返回 goal_met=True + note，
  判断器故障绝不吞掉已完成的分析结果。
- 输入 digest 紧凑（防 token 膨胀）：报告 markdown 仅取前 1200 字符。
- model 经 BaseAgent 注入可 mock（离线测试）。
"""

GOAL_PROMPT = """你是目标达成评估器。判断一次数据分析是否真正达成了用户的原始目标。

## 用户目标
{goal}

## 分析结果摘要
{digest}

## 判断要求
1. 严格对照「用户目标」与「结果摘要」，不要因为分析本身完整就判定达成。
   例：用户问「找出销售额下降的原因」，结果只给了趋势图——虽然分析完整，
   但目标（原因）未达成，goal_met 应为 false。
2. gap 用 1-2 句话说明差距；suggested_followup 给出 1 条可执行的下一步建议。
3. 只输出 JSON，不要其他文本。

## 输出格式
{{"goal_met": true 或 false, "gap": "差距描述", "suggested_followup": "下一步建议"}}
"""

# 仅 goal_met 必填；gap/suggested_followup 缺失由调用方补默认（不因缺描述字段判失败）
GOAL_SCHEMA: dict = {
    "type": "object",
    "required": ["goal_met"],
    "properties": {
        "goal_met": {"type": "bool"},
        "gap": {"type": "str"},
        "suggested_followup": {"type": "str"},
    },
}


def _fail_open(note: str) -> dict:
    return {
        "goal_met": True,
        "gap": "",
        "suggested_followup": "",
        "note": note,
    }


class GoalEvaluator:
    """无状态评估器：一次调用产出一份 goal_check。用组合而非继承 BaseAgent，
    便于在 planner.run() 内按配置按需构造（每次 run 一个实例，无跨请求状态）。"""

    def __init__(self, model=None, user_id=None):
        # 复用 BaseAgent 的 LLM 解析/调用能力；model 显式注入时跳过 factory
        from agents.base import BaseAgent

        self._agent = BaseAgent(user_id=user_id, model=model)

    def evaluate(self, goal: str, digest: dict) -> dict:
        """评估目标是否达成。任何异常/schema 失败 → fail-open。"""
        digest_text = "\n".join(f"{k}: {v}" for k, v in (digest or {}).items()
                                if v not in (None, "", [], {}))
        if not digest_text:
            return _fail_open("no digest")
        messages = [{"role": "user", "content": GOAL_PROMPT.format(
            goal=goal or "", digest=digest_text[:3000])}]
        try:
            out = self._agent._call_llm_with_schema(messages, GOAL_SCHEMA)
        except Exception as e:
            return _fail_open(f"evaluator error: {e}")
        if out is None:
            return _fail_open("schema validation failed after retries")
        return {
            "goal_met": bool(out.get("goal_met", True)),
            "gap": str(out.get("gap", "") or ""),
            "suggested_followup": str(out.get("suggested_followup", "") or ""),
        }
