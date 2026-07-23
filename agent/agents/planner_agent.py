"""
Planner Agent: 任务规划与编排 —— 理解用户需求，拆解任务，调度 Agent 执行。
"""

import json
import os
import sys
import traceback
from typing import Any

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from agents.base import BaseAgent
from agents.sql_agent import SQLAgent
from agents.trend_agent import TrendAgent
from agents.product_agent import ProductAgent
from agents.risk_agent import RiskAgent
from agents.visualization_agent import VisualizationAgent
from agents.report_agent import ReportAgent
from agents.export_agent import ExportAgent
from utils.logger_handler import logger
from memory.short_term import ConversationMemory, get_session

try:
    from database.data_resolver import DataResolver
except ModuleNotFoundError:
    from agent.database.data_resolver import DataResolver

try:
    from utils.progress_emitter import get_progress_emitter
except ModuleNotFoundError:
    from agent.utils.progress_emitter import get_progress_emitter


class RequestContext:
    """单次分析请求的上下文，按 user_id 隔离数据层，替代旧的实例属性。

    旧设计把 csv_path/dataset_name 存在 PlannerAgent 单例实例属性上，
    多用户并发会互相覆盖；改为每次请求构造局部 ctx 下传，天然隔离。
    """

    __slots__ = ("user_id", "session_id", "csv_path", "dataset_name", "dataset_desc")

    def __init__(self, user_id: str = "default", session_id: str = "",
                 csv_path: str = "", dataset_name: str = "",
                 dataset_desc: str = ""):
        self.user_id = user_id or "default"
        self.session_id = session_id
        self.csv_path = csv_path
        self.dataset_name = dataset_name
        self.dataset_desc = dataset_desc


PLANNER_SYSTEM_PROMPT = """你是一个 AI 数据分析系统的任务规划器。根据用户的问题，制定分析计划。

## 可用的分析能力
1. sql_query: 查询数据库获取数据（必须第一步）
2. trend_analysis: 趋势分析（月度销售/利润趋势，增长率，异常月份）
3. product_analysis: 产品分析（TOP产品，低利润产品，类别分析）
4. risk_analysis: 风险分析（异常检测，区域异常，类别亏损）
5. visualization: 生成图表（趋势图、柱状图、饼图、热力图、散点图）
6. report: 生成 Markdown 分析报告
7. export: 导出报告为 Word/PDF/HTML

## 规则
1. sql_query 必须是计划中的第一步，因为所有后续分析都依赖数据。
2. 根据用户问题选择合适的分析类型。
3. 趋势分析和产品分析通常都需要。
4. 风险分析在用户提到"异常"、"风险"、"下降"、"问题"时加入。
5. 图表生成和报告生成通常放在最后。
6. 输出标准 JSON 格式的执行计划。

## 用户常见问题类型
- "销售额趋势" → sql + trend + visualization + report
- "哪个产品卖得最好" → sql + product + visualization + report
- "分析利润下降原因" → sql + trend + product + risk + visualization + report
- "生成分析报告" → sql + trend + product + risk + visualization + report + export
- "各区域表现如何" → sql + product + risk + report

## 输出格式
{
  "plan": [
    {"step": 1, "agent": "sql_query", "task": "查询某某数据", "depends_on": []},
    {"step": 2, "agent": "trend_analysis", "task": "分析月度趋势", "depends_on": [1]},
    ...
  ],
  "reasoning": "为什么这样安排",
  "title": "报告标题建议"
}

只输出 JSON，不要有其他文本。
"""


AGENT_LABELS = {
    "sql_query": "SQL 查询",
    "trend_analysis": "趋势分析",
    "product_analysis": "产品分析",
    "risk_analysis": "风险分析",
    "visualization": "图表生成",
    "report": "生成报告",
    "export": "导出报告",
}


class PlannerAgent(BaseAgent):
    """任务规划与编排 Agent：整个系统的入口和协调器。"""

    name = "planner_agent"

    def __init__(self, user_id=None):
        super().__init__(user_id)   # self.model = 该用户的 LLM（按 user_id 缓存）
        self.sql_agent = SQLAgent()
        self.trend_agent = TrendAgent()
        self.product_agent = ProductAgent()
        self.risk_agent = RiskAgent()
        self.viz_agent = VisualizationAgent()
        self.report_agent = ReportAgent()
        self.export_agent = ExportAgent()
        # 子 Agent 默认按「默认配置」构建模型；这里统一指向本用户的模型，
        # 实现整条流水线按 user_id 隔离 LLM 配置（避免改每个子 Agent 的构造签名）。
        _model = self.model
        for _ag in (self.sql_agent, self.trend_agent, self.product_agent,
                    self.risk_agent, self.viz_agent, self.report_agent, self.export_agent):
            _ag.model = _model
        # 不再保存请求级状态（_current_csv_path/_current_dataset_name/_last_loaded_csv），
        # 改为每次请求用 RequestContext 局部变量下传，避免多用户并发竞态。

        self._agent_map = {
            "sql_query": self._run_sql,
            "trend_analysis": self._run_trend,
            "product_analysis": self._run_product,
            "risk_analysis": self._run_risk,
            "visualization": self._run_visualization,
            "report": self._run_report,
            "export": self._run_export,
        }

    def _resolve_context(self, input_data: dict) -> RequestContext:
        """从 input_data 解析 user_id/session_id，并探测数据集，构造请求上下文。

        DuckDB 实例按 user_id 隔离：init_duckdb(user_id, csv_path) 内部会缓存实例，
        并在需要切换 CSV 时 reload（每个实例独立 :memory: 连接，无跨用户竞态）。
        """
        user_id = input_data.get("user_id") or "default"
        session_id = input_data.get("session_id", "")

        try:
            resolved = DataResolver.resolve(input_data.get("query", ""), user_id=user_id)
            csv_path = resolved.get("csv_path", "")
            dataset_name = resolved.get("name", "Unknown Dataset")
            dataset_desc = resolved.get("description", "")
        except Exception as e:
            logger.warning(f"DataResolver failed: {e}, using default dataset")
            csv_path = ""
            dataset_name = "Online Shopping Dataset"
            dataset_desc = ""

        ctx = RequestContext(user_id=user_id, session_id=session_id,
                             csv_path=csv_path, dataset_name=dataset_name,
                             dataset_desc=dataset_desc)

        # 确保该 user 的 DuckDB 实例加载了本次所需 CSV（实例内部按 last_loaded_csv 判重）
        if csv_path and os.path.exists(csv_path):
            try:
                from database.duckdb_manager import init_duckdb
            except ModuleNotFoundError:
                from agent.database.duckdb_manager import init_duckdb
            init_duckdb(csv_path=csv_path, user_id=user_id)

        if dataset_name:
            logger.info(f"Planner using dataset: {dataset_name} ({csv_path}) for user={user_id}")
        return ctx

    @staticmethod
    def _emit_progress(event_type: str, data: dict | None = None) -> None:
        """向当前请求的进度通道发射事件（无通道时 no-op，如同步 /api/analysis 路径）。"""
        try:
            emitter = get_progress_emitter()
            if emitter is not None:
                emitter.emit(event_type, data)
        except Exception:
            pass

    def run(self, input_data: dict) -> dict:
        """
        input_data = {
            "query": "用户自然语言问题",
            "history": [{"role": "user"|"assistant", "content": str}] (optional),
            "user_id": "u_...",      # optional, 决定数据层隔离
            "session_id": "...",     # optional
        }
        returns: 完整分析结果，包含 report 和 export 信息
        """
        query = input_data.get("query", "")
        history = input_data.get("history", [])
        if not query:
            return {"error": "No query provided"}

        # 0. 解析请求上下文（按 user_id 隔离数据集，替代旧的实例属性）
        ctx = self._resolve_context(input_data)

        plan_data = self._create_plan(query, history)
        plan = plan_data.get("plan", [])
        title = plan_data.get("title", "数据分析报告")
        reasoning = plan_data.get("reasoning", "")

        if not plan:
            # 如果 LLM 规划失败，使用默认计划
            plan = self._default_plan(query)
            title = "数据分析报告"

        logger.info(f"Planner created plan with {len(plan)} steps: {[s['agent'] for s in plan]}")

        # 进度：把完整计划下发给前端，供步骤清单渲染（无 emitter 时 no-op）
        self._emit_progress("plan", {
            "title": title,
            "steps": [
                {
                    "step": s.get("step"),
                    "agent": s.get("agent", ""),
                    "label": AGENT_LABELS.get(s.get("agent", ""), s.get("agent", "")),
                    "task": s.get("task", query),
                }
                for s in plan
            ],
        })

        # 2. 按计划执行
        results = {}
        errors = []

        for step in plan:
            agent_name = step.get("agent", "")
            task = step.get("task", query)
            depends = step.get("depends_on", [])

            # 检查依赖是否完成
            skip = False
            for dep in depends:
                dep_key = f"step_{dep}"
                if dep_key not in results or results[dep_key] is None:
                    logger.warning(f"Step {step.get('step')} depends on step {dep} which is not ready")
                    skip = True

            if skip:
                continue

            step_key = f"step_{step.get('step')}"
            logger.info(f"Executing step {step.get('step')}: {agent_name} - {task}")

            self._emit_progress("step_start", {"step": step.get("step")})
            step_ok = False
            try:
                handler = self._agent_map.get(agent_name)
                if handler:
                    step_result = handler(task, results, ctx)
                    results[step_key] = step_result
                    results[f"{agent_name}_result"] = step_result
                    step_ok = True
                else:
                    errors.append(f"Unknown agent: {agent_name}")
                    results[step_key] = None
            except Exception as e:
                logger.error(f"Step {step.get('step')} ({agent_name}) failed: {e}")
                logger.error(traceback.format_exc())
                errors.append(f"Step {step.get('step')} failed: {e}")
                results[step_key] = None
            # 仅成功才标记完成；失败（异常或未知 agent）标记 error，
            # 避免 UI 把失败步骤误显示为 ✓（前端 step-progress step_error 分支）
            if step_ok:
                self._emit_progress("step_done", {"step": step.get("step")})
            else:
                self._emit_progress("step_error", {"step": step.get("step")})

        # 3. 汇总结果
        return {
            "query": query,
            "title": title,
            "reasoning": reasoning,
            "plan": plan,
            "results": results,
            "errors": errors,
            "report": results.get("report_result", results.get("report", {})),
            "exports": results.get("export_result", results.get("export", {})),
            "dataset": {
                "name": ctx.dataset_name,
                "csv_path": ctx.csv_path,
            },
            "success": len(errors) == 0,
        }

    def _format_history(self, history: list[dict]) -> str:
        """将历史对话格式化为文本。"""
        if not history:
            return ""
        lines = []
        for h in history[-20:]:  # 最多保留最近 20 条
            role = "用户" if h.get("role") == "user" else "助手"
            content = str(h.get("content", ""))[:300]
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def run_stream(self, input_data: dict):
        """
        流式执行版本：yield (event_type, data) 元组，支持前端实时展示进度。

        event_type:
          - "status": 状态消息，data 为 str
          - "step_start": 步骤开始，data 为 {"step": int, "agent": str, "task": str}
          - "step_done": 步骤完成，data 为 {"step": int, "agent": str}
          - "report": 报告 markdown 内容，data 为 str
          - "charts": 图表列表，data 为 list
          - "exports": 导出文件列表，data 为 list
          - "done": 全部完成，data 为完整结果 dict
          - "error": 错误消息，data 为 str
        """
        query = input_data.get("query", "")
        if not query:
            yield ("error", "No query provided")
            return

        # 0. 解析请求上下文（按 user_id 隔离数据集）
        ctx = self._resolve_context(input_data)

        # 1. 生成计划
        yield ("status", "正在分析您的问题，制定分析计划...")
        history = input_data.get("history", [])
        plan_data = self._create_plan(query, history)
        plan = plan_data.get("plan", [])
        title = plan_data.get("title", "数据分析报告")

        if not plan:
            plan = self._default_plan(query)
            title = "数据分析报告"

        logger.info(f"Planner created plan with {len(plan)} steps: {[s['agent'] for s in plan]}")

        # 2. 按计划逐步执行
        results = {}
        errors = []

        for step in plan:
            agent_name = step.get("agent", "")
            task = step.get("task", query)
            depends = step.get("depends_on", [])

            # 检查依赖
            skip = False
            for dep in depends:
                dep_key = f"step_{dep}"
                if dep_key not in results or results[dep_key] is None:
                    logger.warning(f"Step {step.get('step')} depends on step {dep} which is not ready")
                    skip = True

            if skip:
                continue

            step_num = step.get("step")
            step_key = f"step_{step_num}"

            yield ("step_start", {"step": step_num, "agent": agent_name, "task": task})
            yield ("status", f"步骤 {step_num}: {task}...")

            try:
                handler = self._agent_map.get(agent_name)
                if handler:
                    step_result = handler(task, results, ctx)
                    results[step_key] = step_result
                    results[f"{agent_name}_result"] = step_result
                    yield ("step_done", {"step": step_num, "agent": agent_name})
                else:
                    errors.append(f"Unknown agent: {agent_name}")
                    results[step_key] = None
                    yield ("step_done", {"step": step_num, "agent": agent_name})
            except Exception as e:
                logger.error(f"Step {step_num} ({agent_name}) failed: {e}")
                logger.error(traceback.format_exc())
                errors.append(f"Step {step_num} failed: {e}")
                results[step_key] = None
                yield ("step_done", {"step": step_num, "agent": agent_name})

        # 3. 输出图表
        viz_result = results.get("visualization_result", {})
        charts = viz_result.get("charts", []) if viz_result else []
        if charts:
            yield ("charts", charts)

        # 4. 输出导出文件
        export_result = results.get("export_result", {})
        export_files = export_result.get("files", []) if export_result else []
        if export_files:
            yield ("exports", export_files)

        # 5. 输出最终报告
        report = results.get("report_result", results.get("report", {}))
        markdown = report.get("markdown", "")
        if markdown:
            yield ("report", markdown)

        # 6. 完成
        final_result = {
            "query": query,
            "title": title,
            "plan": plan,
            "results": results,
            "errors": errors,
            "report": report,
            "exports": export_result,
            "dataset": {
                "name": ctx.dataset_name,
                "csv_path": ctx.csv_path,
            },
            "success": len(errors) == 0,
        }
        yield ("done", final_result)

    # ── Agent 执行方法 ──

    def _run_sql(self, task: str, prev_results: dict, ctx: "RequestContext") -> dict:
        """执行 SQL 查询（按 ctx.user_id 隔离数据层）。"""
        return self.sql_agent.run({"task": task, "user_id": ctx.user_id})

    def _run_trend(self, task: str, prev_results: dict, ctx: "RequestContext") -> dict:
        """执行趋势分析。"""
        sql_data = prev_results.get("sql_query_result", {})
        df_json = sql_data.get("dataframe_json", "[]")
        if not df_json or df_json == "[]":
            return {"error": "No data from SQL agent for trend analysis"}
        return self.trend_agent.run({
            "dataframe_json": df_json,
            "task": task,
        })

    def _run_product(self, task: str, prev_results: dict, ctx: "RequestContext") -> dict:
        """执行产品分析。"""
        sql_data = prev_results.get("sql_query_result", {})
        df_json = sql_data.get("dataframe_json", "[]")
        if not df_json or df_json == "[]":
            return {"error": "No data from SQL agent for product analysis"}
        return self.product_agent.run({
            "dataframe_json": df_json,
            "task": task,
        })

    def _run_risk(self, task: str, prev_results: dict, ctx: "RequestContext") -> dict:
        """执行风险分析。"""
        sql_data = prev_results.get("sql_query_result", {})
        df_json = sql_data.get("dataframe_json", "[]")
        if not df_json or df_json == "[]":
            return {"error": "No data from SQL agent for risk analysis"}
        return self.risk_agent.run({
            "dataframe_json": df_json,
            "task": task,
        })

    def _run_visualization(self, task: str, prev_results: dict, ctx: "RequestContext") -> dict:
        """生成图表。"""
        sql_data = prev_results.get("sql_query_result", {})
        df_json = sql_data.get("dataframe_json", "[]")
        extra = {
            "trend_summary": prev_results.get("trend_analysis_result", {}),
            "product_summary": prev_results.get("product_analysis_result", {}),
        }
        return self.viz_agent.run({
            "dataframe_json": df_json,
            "task": task,
            "extra_data": extra,
        })

    def _run_report(self, task: str, prev_results: dict, ctx: "RequestContext") -> dict:
        """生成报告。"""
        charts = prev_results.get("visualization_result", {}).get("charts", [])
        return self.report_agent.run({
            "task": task,
            "sql_result": prev_results.get("sql_query_result", {}),
            "trend_result": prev_results.get("trend_analysis_result", {}),
            "product_result": prev_results.get("product_analysis_result", {}),
            "risk_result": prev_results.get("risk_analysis_result", {}),
            "charts": charts,
            "title": task,
        })

    def _run_export(self, task: str, prev_results: dict, ctx: "RequestContext") -> dict:
        """导出报告。"""
        report = prev_results.get("report_result", {})
        markdown = report.get("markdown", "")
        title = report.get("title", "分析报告")
        if not markdown:
            return {"error": "No report content to export", "files": []}
        return self.export_agent.run({
            "markdown": markdown,
            "title": title,
            "formats": ["md", "html"],
        })

    # ── 计划生成 ──

    def _create_plan(self, query: str, history: list[dict] | None = None) -> dict:
        """使用 LLM 生成执行计划。"""
        messages = [{"role": "system", "content": PLANNER_SYSTEM_PROMPT}]
        # 注入历史上下文
        if history:
            history_text = self._format_history(history)
            if history_text:
                messages.append({
                    "role": "system",
                    "content": f"[之前的对话历史]\n{history_text}\n请结合以上历史上下文理解用户的新问题。",
                })
        messages.append({"role": "user", "content": f"请为以下用户问题制定分析计划：\n{query}"})
        try:
            response = self._call_llm(messages)
            return self._parse_json(response)
        except Exception as e:
            logger.warning(f"Plan generation failed: {e}")
            return {}

    def _default_plan(self, query: str) -> list[dict]:
        """当 LLM 规划失败时的默认计划。"""
        query_lower = query.lower()
        plan = [
            {"step": 1, "agent": "sql_query", "task": query, "depends_on": []},
        ]
        step = 2

        # 根据关键词自动添加步骤
        if any(w in query_lower for w in ["趋势", "trend", "增长", "下降", "变化", "趋势", "月度", "monthly"]):
            plan.append({"step": step, "agent": "trend_analysis", "task": "趋势分析", "depends_on": [1]})
            step += 1

        if any(w in query_lower for w in ["产品", "product", "top", "卖", "销量", "利润"]):
            plan.append({"step": step, "agent": "product_analysis", "task": "产品分析", "depends_on": [1]})
            step += 1

        if any(w in query_lower for w in ["风险", "risk", "异常", "anomaly", "问题", "下降"]):
            plan.append({"step": step, "agent": "risk_analysis", "task": "风险分析", "depends_on": [1]})
            step += 1

        if any(w in query_lower for w in ["图", "chart", "visual", "可视化"]):
            plan.append({"step": step, "agent": "visualization", "task": "图表生成", "depends_on": [step - 1]})
            step += 1

        if any(w in query_lower for w in ["报告", "report", "分析", "总结", "汇总"]):
            plan.append({"step": step, "agent": "report", "task": "生成分析报告", "depends_on": [step - 1]})
            step += 1

        # 确保至少有 trend + product + report 作为最完整的分析
        if len(plan) <= 1:
            plan.extend([
                {"step": 2, "agent": "trend_analysis", "task": "趋势分析", "depends_on": [1]},
                {"step": 3, "agent": "product_analysis", "task": "产品分析", "depends_on": [1]},
                {"step": 4, "agent": "visualization", "task": "图表生成", "depends_on": [2, 3]},
                {"step": 5, "agent": "report", "task": "生成分析报告", "depends_on": [4]},
            ])

        return plan
