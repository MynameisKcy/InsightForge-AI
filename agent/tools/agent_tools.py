import csv
import json
from datetime import date

from langchain_core.tools import tool
import random

from rag.rag_service import RagSummarizerService
from utils.cancel_token import PipelineCancelledError
from utils.config_handler import agent_conf
from utils.path_tool import get_abs_path
from agents.planner_agent import PlannerAgent

rag = RagSummarizerService()

user_ids = ["1001","1002","1003","1004","1005","1006","1007","1008","1009","1010"]


def _external_data_path() -> str:
    return get_abs_path(agent_conf["external_data_path"])


@tool(description="从向量库中检索参考资料，以纯字符形式返回")
def rag_sumarize(query:str) -> str:
    # owner 隔离：从请求上下文取当前 user_id，仅检索该用户 + 公共 system 知识
    return rag.rag_summarize(query, _current_user_id())

@tool(description="获取城市天气")
def get_weather(city:str) -> str:
    return f"城市{city}天气为晴天，气温25摄氏度，空气适度50%"

@tool(description="获取用户所在城市，以纯字符形式返回")
def get_user_location() -> str:
    return random.choice(["深圳","青岛","北京"])

@tool(description="获取用户ID，，以纯字符形式返回")
def get_user_id() -> str:
    return random.choice(user_ids)

@tool(description="获取当前月份，以纯字符形式返回")
def get_current_month() -> str:
    return str(date.today().month)


@tool(description="从外部系统中获取指定用户指定月份的使用记录，以纯字符串形式返回，如果未检索到则返回未找到提示")
def get_external_data(user_id: str, month: str) -> str:
    with open(_external_data_path(), "r", encoding="utf-8", newline="") as records_file:
        for record in csv.DictReader(records_file):
            if record["user_id"] == str(user_id) and record["month"] == str(month):
                return (
                    f"用户ID:{record['user_id']}，月份:{record['month']}，"
                    f"使用情况:{record['使用情况']}，效率:{record['效率']}，问题:{record['问题']}"
                )
    return f"未找到用户ID:{user_id}在月份:{month}的外部使用记录"

@tool(description="fill context for report，无入参，无返回值，调用后触发中间件自动为报告生成的场景动态注入上下文信息，为后续提示词切换提供上下文信息")
def fill_report_context_for_report():
    return "fill context for report has worded"


# ── 数据分析桥接工具：使智能客服可以调用多 Agent 分析系统 ──

# PlannerAgent 实例按 user_id 缓存：各用户独立实例，用自己的 LLM 配置。
# 旧实现是进程级单例（PlannerAgent() 不传 user_id）-> 永远用默认模型配置，
# 导致 per-user LLM 配置在数据分析链路失效。详见 docs/adr/0001-single-entry-analysis-as-tool.md
_analyst_cache = {}  # user_id -> PlannerAgent


def _get_or_create_analyst(user_id: str | None = None):
    """按 user_id 缓存 PlannerAgent 实例（首次取用时构建）。

    user_id 决定该实例使用的 LLM 配置（见 factory.get_chat_model）：传入真实 user_id
    才会按用户配置构建模型，而非默认配置。与 ReactAgent 一样按用户隔离，
    配置变更时通过 invalidate_analyst 丢弃实例、下次重建。
    """
    key = user_id or "default"
    if key not in _analyst_cache:
        _analyst_cache[key] = PlannerAgent(user_id=key)
    return _analyst_cache[key]


def invalidate_analyst(user_id: str | None = None) -> None:
    """丢弃缓存的 PlannerAgent 实例，下次取用时按新配置重建。

    user_id=None -> 清空全部；否则只清该用户。配合 api.deps._invalidate_user_agents
    与 factory.reload_model_config 一并清掉 Agent 实例与模型缓存，使新配置真正生效。
    """
    if user_id is None:
        _analyst_cache.clear()
    else:
        _analyst_cache.pop(user_id or "default", None)


@tool(description="运行完整的数据分析流程（SQL查询→趋势分析→分组对比分析→可视化图表→报告）。系统会自动生成图表并嵌入对话，无需你自行绘图。参数 query 为自然语言分析需求，例如'分析各月销售趋势'、'各区人口分布对比'、'可视化路口流量变化'、'画一幅趋势图'")
def run_full_analysis(query: str) -> str:
    """运行完整的数据分析流程并返回文本结论。"""
    try:
        from utils.request_context import get_user_id, get_session_id
        analyst = _get_or_create_analyst(get_user_id())
        result = analyst.run({
            "query": query,
            "user_id": get_user_id(),
            "session_id": get_session_id(),
        })
        if not result.get("success", False):
            errors = result.get("errors", [])
            return f"分析过程出现错误: {'; '.join(errors)}"

        report = result.get("report", {})
        markdown = report.get("markdown", "")
        if markdown:
            # 截取前 3000 字符避免 token 超限
            return markdown[:3000] + ("..." if len(markdown) > 3000 else "")
        return "分析已完成，但未生成报告内容。"
    except PipelineCancelledError:
        # 客户端断连取消不吞：向上传播结束生产者线程，避免 Agent 循环重试
        raise
    except Exception as e:
        return f"数据分析调用失败: {str(e)}"


@tool(description="快速查询数据集概况，返回所有已加载数据集的表结构、行数、关键统计信息，无需参数")
def get_data_overview() -> str:
    """返回所有数据集的概况信息，自动适配当前数据集的实际列名。"""
    from database.duckdb_manager import init_duckdb
    from database.safety import safe_ident
    from utils.request_context import get_user_id

    try:
        db = init_duckdb(user_id=get_user_id())
        tables = db.get_table_names()

        if not tables:
            return "当前没有加载任何数据集。请上传 CSV/Excel 文件开始分析。"

        all_parts = []
        for table_name in tables:
            try:
                qname = safe_ident(table_name)
                row_count = db.query_df(f"SELECT COUNT(*) AS cnt FROM {qname}").iloc[0, 0]
                cols_info = db.execute(f"DESCRIBE {qname}").fetchall()
                orig_cols = [c[0] for c in cols_info]

                parts = [
                    f"📊 数据集: {table_name}",
                    f"- 总记录数: {row_count} 条",
                    f"- 字段数: {len(cols_info)} 个",
                    f"- 全部列名: {', '.join(orig_cols)}",
                ]
                all_parts.append("\n".join(parts))
            except Exception as e:
                all_parts.append(f"📊 数据集: {table_name} (读取失败: {e})")

        return "\n\n".join(all_parts)
    except Exception as e:
        return f"数据查询失败: {str(e)}"


@tool(description="针对特定分组或趋势问题进行快速分析，返回分析结论。参数 query 为具体问题，如'哪个地区人口最多'、'最近几期数值是否下降'")
def quick_data_insight(query: str) -> str:
    """快速数据分析，返回关键洞察。"""
    from agents.sql_agent import SQLAgent
    from agents.trend_agent import TrendAgent

    from utils.request_context import get_user_id

    try:
        # user_id 必须传入构造器：Agent 的 LLM 按用户解析（网页设置 > .env），
        # 不传则钉死 .env 默认模型（免费额度耗尽时 403）。
        uid = get_user_id()
        sql = SQLAgent(user_id=uid)
        sql_result = sql.run({"task": query, "user_id": uid})
        if sql_result.get("error"):
            return f"数据查询失败: {sql_result['error']}"

        df_json = sql_result.get("dataframe_json", "[]")
        row_count = sql_result.get("row_count", 0)

        if row_count == 0:
            return "查询未返回任何数据，请确认问题是否合理。"

        if row_count > 1:
            trend = TrendAgent(user_id=uid)
            trend_result = trend.run({"dataframe_json": df_json})
            insight = trend_result.get("insight", trend_result.get("trend_summary", ""))
            if insight:
                return f"分析结论（共 {row_count} 条数据）:\n{insight}"

        # 如果只有少量行，直接返回数据摘要
        import pandas as pd
        from io import StringIO
        df = pd.read_json(StringIO(df_json), orient="records")
        return f"查询结果（共 {row_count} 条）:\n{df.head(10).to_string(index=False)}"
    except Exception as e:
        return f"快速分析失败: {str(e)}"


@tool(description="从图表知识库和外部搜索中获取分析建议。结合历史图表数据和外部信息，为当前分析提供深度洞察。参数 query 为分析问题，如'销售下降原因分析'、'如何优化产品定价'")
def get_chart_insights(query: str) -> str:
    """从图表知识库检索历史图表数据，结合外部搜索生成分析建议。"""
    from rag.chart_knowledge import chart_knowledge

    parts = []

    # 1. 从图表知识库检索相关历史图表
    try:
        chart_context = chart_knowledge.get_chart_context_for_rag(query, max_charts=5)
        if chart_context and "暂无" not in chart_context:
            parts.append(chart_context)
    except Exception as e:
        logger = __import__('logging').getLogger(__name__)
        logger.warning(f"Chart knowledge retrieval failed: {e}")

    # 外部搜索由 LLM 自主调用，此处仅返回内部知识库的结果

    if parts:
        return "\n\n".join(parts)
    return "未找到相关的历史图表分析数据。建议先运行数据分析生成图表，积累知识库后再查询。"


@tool(description="查询持久化的客户数据概况。返回按订单数排名的 TOP 客户列表，包含客户ID、名称、所在城市、区域、部门、订单数等信息。参数 top_n 为返回数量，默认 10")
def get_customer_overview_tool(top_n: int = 10) -> str:
    """查询已持久化的客户数据，返回 TOP N 客户概况（仅当前用户）。"""
    from database.customer_profiles import get_customer_overview
    try:
        from utils.request_context import get_user_id
        uid = get_user_id()
    except Exception:
        uid = "default"

    customers = get_customer_overview(uid, top_n)
    if not customers:
        return "暂无持久化的客户数据。请先加载包含客户信息的数据集（如 train.csv），系统会自动提取并存储客户数据。"

    lines = [f"TOP {len(customers)} 客户（按订单数排名）:"]
    for i, c in enumerate(customers, 1):
        parts = [f"{i}. 客户ID: {c['customer_id']}"]
        if c.get("customer_name"):
            parts.append(f"名称: {c['customer_name']}")
        if c.get("segment"):
            parts.append(f"部门: {c['segment']}")
        if c.get("city"):
            parts.append(f"城市: {c['city']}")
        if c.get("region"):
            parts.append(f"区域: {c['region']}")
        parts.append(f"订单数: {c['order_count']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


@tool(description="获取持久化客户数据的统计信息。返回总客户数、按城市分布、按部门分布等汇总统计，无需参数")
def get_customer_stats_tool() -> str:
    """获取客户数据统计汇总（仅当前用户）。"""
    from database.customer_profiles import get_customer_count
    try:
        from utils.request_context import get_user_id
        uid = get_user_id()
    except Exception:
        uid = "default"

    stats = get_customer_count(uid)
    total = stats.get("total_customers", 0)
    if total == 0:
        return "暂无持久化的客户数据。请先加载包含客户信息的数据集。"

    lines = [f"客户数据统计: 共 {total} 位客户"]

    by_city = stats.get("by_city", [])
    if by_city:
        lines.append("\n按城市分布 (TOP 10):")
        for c in by_city:
            lines.append(f"  - {c['city']}: {c['cnt']} 位客户")

    by_segment = stats.get("by_segment", [])
    if by_segment:
        lines.append("\n按部门分布:")
        for s in by_segment:
            lines.append(f"  - {s['segment']}: {s['cnt']} 位客户")

    return "\n".join(lines)


# ── 需求③：文件引用与文本报告 ──
import json as _json

from utils.request_context import get_user_id as _get_user_id_ctx


def _current_user_id() -> str:
    try:
        return _get_user_id_ctx()
    except Exception:
        return "default"


def _list_text_files(user_id: str):
    """列出文本类知识库文件（PDF/Word/TXT/MD，进 Chroma）。"""
    from utils.config_handler import chroma_conf
    from utils.file_handler import get_file_md5_hex
    from utils.path_tool import get_abs_path
    import os as _os
    uid = user_id or _current_user_id()
    data_dir = _os.path.join(get_abs_path(chroma_conf["data_path"]), uid)
    allowed = tuple(chroma_conf.get("allowed_knowledge_file_type", ["txt", "pdf", "docx", "md"]))
    files = []
    if _os.path.isdir(data_dir):
        for fname in sorted(_os.listdir(data_dir)):
            fpath = _os.path.join(data_dir, fname)
            if not _os.path.isfile(fpath):
                continue
            ext = _os.path.splitext(fname)[1].lower().lstrip(".")
            if ext not in allowed:
                continue
            files.append({"name": fname, "type": "text",
                          "status": "已完成", "size": _os.path.getsize(fpath),
                          "path": fpath})
    return files


def _list_table_files(user_id: str):
    """列出表格类数据集（CSV/Excel，进 DuckDB）。"""
    from database.datasources_db import datasources_db
    try:
        return datasources_db.list_datasets(owner_user_id=user_id) or []
    except Exception:
        return []


def _new_document_report_agent():
    from agents.document_report_agent import DocumentReportAgent
    # user_id 必须取当前请求用户（工具线程内 contextvar 已设）：Agent 的 LLM
    # 按用户解析（网页设置 > .env），不传则钉死 .env 默认模型。
    return DocumentReportAgent(user_id=_current_user_id())


@tool(description="列出当前用户已上传的所有文件（文本类与表格类），含文件名、类型(text/table)、表名、状态。当用户要求基于某文件生成报告/分析时，先调用此工具确认可用文件。返回 JSON 字符串。")
def list_user_files() -> str:
    uid = _current_user_id()
    files = []
    for f in _list_text_files(uid):
        files.append({"name": f.get("name"), "type": "text",
                      "status": f.get("status", "已完成"),
                      "path": f.get("path")})
    for d in _list_table_files(uid):
        files.append({"name": d.get("name"), "type": "table",
                      "table_name": d.get("table_name"),
                      "status": "已完成"})
    return _json.dumps(files, ensure_ascii=False)


@tool(description="对指定的文本类文件（PDF/Word/TXT/MD）生成结构化报告（摘要+关键要点+问答）。file_path 为文件完整路径，question 为可选问题（留空则自动生成要点问答）。返回 Markdown 字符串。")
def document_report(file_path: str, question: str = "") -> str:
    try:
        agent = _new_document_report_agent()
        result = agent.run(file_path, question=question or None)
        return result["markdown"]
    except Exception as e:
        return f"报告生成失败：{e}"