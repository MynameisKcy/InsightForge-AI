import time
import os
import sys

# ── 方案C：在导入任何会实例化模型的模块之前，先加载 .env（DASHSCOPE_API_KEY）──
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
except ImportError:
    pass

import streamlit as st

# 确保 agent 目录在 path 中
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if CURRENT_DIR not in sys.path:
    sys.path.insert(0, CURRENT_DIR)

from agent.react_agent import ReactAgent
from agents.planner_agent import PlannerAgent
from utils.report_exporter import build_report_filename, is_report_content, to_markdown_bytes
from memory.short_term import ConversationMemory, get_session

st.set_page_config(page_title="AI Data Analyst", page_icon="🤖", layout="wide")

# ── 侧边栏：模式切换 ──
with st.sidebar:
    st.title("🔧 功能面板")
    st.divider()

    if "mode" not in st.session_state:
        st.session_state["mode"] = "智能客服"

    previous_mode = st.session_state["mode"]
    mode = st.radio(
        "选择模式",
        ["智能客服", "数据分析"],
        index=0 if st.session_state["mode"] == "智能客服" else 1,
        help="智能客服：数据分析顾问，趋势解读 + 商业建议 | 数据分析：多 Agent 协作，图表 + 报告生成",
    )
    st.session_state["mode"] = mode

    # 模式切换时清空对话历史和短期记忆
    if mode != previous_mode:
        st.session_state["message"] = []
        st.session_state["analyst"] = None  # 重置分析器
        st.session_state["memory"].clear()  # 清空短期记忆
        st.rerun()

    st.divider()
    st.caption("AI Data Analyst Multi-Agent System")

# ── 标题 ──
if mode == "智能客服":
    st.title("🤖 机器人智能客服")
else:
    st.title("📊 AI 数据分析助手")
st.divider()

# ── 初始化 session ──
if "agent" not in st.session_state:
    st.session_state["agent"] = ReactAgent()

if "analyst" not in st.session_state:
    st.session_state["analyst"] = None  # 延迟初始化

if "message" not in st.session_state:
    st.session_state["message"] = []

if "memory" not in st.session_state:
    # 使用固定的用户 ID 维护当前会话的短期记忆
    st.session_state["memory"] = ConversationMemory(user_id="streamlit_user")


def render_report_download(content: str, key: str):
    if not is_report_content(content):
        return
    st.download_button(
        label="下载 Markdown 报告",
        data=to_markdown_bytes(content),
        file_name=build_report_filename(content),
        mime="text/markdown",
        key=key,
    )


def render_analysis_result(result: dict, container):
    """渲染数据分析结果。"""
    if not result.get("success", False):
        container.error(f"分析过程出现错误: {result.get('errors', [])}")
        return

    # 显示报告内容
    report = result.get("report", {})
    markdown = report.get("markdown", "")
    if markdown:
        container.markdown(markdown)

    # 显示图表
    viz_result = result.get("results", {}).get("visualization_result", {})
    charts = viz_result.get("charts", []) if viz_result else []
    if charts:
        st.divider()
        st.subheader("📈 生成的图表")
        cols = st.columns(min(len(charts), 2))
        for i, chart in enumerate(charts):
            chart_path = chart.get("path", "")
            if chart_path and os.path.exists(chart_path) and chart_path.endswith(".html"):
                with cols[i % 2]:
                    st.caption(chart.get("title", ""))
                    try:
                        with open(chart_path, "r", encoding="utf-8") as f:
                            st.components.v1.html(f.read(), height=450, scrolling=False)
                    except Exception:
                        st.info(f"图表已保存至: {chart_path}")

    # 导出文件
    export_result = result.get("exports", {})
    export_files = export_result.get("files", []) if export_result else []
    if export_files:
        st.divider()
        st.subheader("📥 下载报告")
        for f in export_files:
            fpath = f.get("path", "")
            ffmt = f.get("format", "")
            if fpath and os.path.exists(fpath):
                with open(fpath, "rb") as fb:
                    st.download_button(
                        label=f"下载 {ffmt.upper()} 报告",
                        data=fb.read(),
                        file_name=os.path.basename(fpath),
                        mime={
                            "md": "text/markdown",
                            "html": "text/html",
                            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            "pdf": "application/pdf",
                        }.get(ffmt, "application/octet-stream"),
                        key=f"dl_{ffmt}_{int(time.time())}",
                    )


# ── 渲染历史消息 ──
for index, message in enumerate(st.session_state["message"]):
    with st.chat_message(message["role"]):
        if message["role"] == "assistant" and message.get("mode") == "数据分析":
            render_analysis_result(message.get("analysis_result", {}), st.container())
        else:
            st.write(message["content"])
            if message["role"] == "assistant":
                render_report_download(message["content"], f"download_report_{index}")

# ── 输入处理 ──
prompt = st.chat_input(
    "请输入问题，例如：分析销售趋势，给出优化建议..."
    if mode == "智能客服"
    else "请输入数据分析需求，例如：分析各月销售趋势并生成报告..."
)

if prompt:
    st.chat_message("user").write(prompt)
    st.session_state["message"].append({"role": "user", "content": prompt, "mode": mode})

    if mode == "智能客服":
        # ── 智能客服模式（带短期记忆） ──
        mem = st.session_state["memory"]

        # 先获取历史上下文（不包含当前消息），再添加当前消息到记忆
        history = mem.get_context()
        mem.add_user_message(prompt)

        with st.chat_message("assistant"):
            content_parts = []
            thinking_state = {"active": True}

            # 使用 st.status 显示持久化的转圈动画 + 思考状态
            with st.status("🤔 正在思考...", expanded=False) as thinking_status:

                def capture_with_thinking(generator):
                    """过滤 [THINKING] 消息并更新思考状态，流式输出实际内容。"""
                    for chunk in generator:
                        if isinstance(chunk, str) and chunk.startswith("[THINKING]"):
                            thinking_text = chunk.replace("[THINKING]", "").strip()
                            thinking_status.update(label=f"🤔 {thinking_text}")
                        else:
                            if thinking_state["active"]:
                                # 首次出现实际内容时，收起思考状态
                                thinking_status.update(label="✅ 思考完成", state="complete", expanded=False)
                                thinking_state["active"] = False
                            content_parts.append(chunk)
                            for char in chunk:
                                time.sleep(0.01)
                                yield char

                res_stream = st.session_state["agent"].execute_stream(prompt, history=history)
                rendered_response = st.write_stream(capture_with_thinking(res_stream))

            assistant_content = rendered_response if isinstance(rendered_response, str) else ""
            if not assistant_content:
                assistant_content = "".join(content_parts)
            assistant_content = assistant_content.strip()

            if assistant_content:
                # 记录助手回复到短期记忆
                mem.add_assistant_message(assistant_content)

                render_report_download(
                    assistant_content,
                    f"download_report_{len(st.session_state['message'])}",
                )
                st.session_state["message"].append({
                    "role": "assistant",
                    "content": assistant_content,
                    "mode": "智能客服",
                })

    else:
        # ── 数据分析模式（流式输出 + 短期记忆） ──
        mem = st.session_state["memory"]

        # 先获取历史上下文（不包含当前消息），再添加当前消息到记忆
        history = mem.get_context()
        mem.add_user_message(prompt)

        if st.session_state["analyst"] is None:
            st.session_state["analyst"] = PlannerAgent()

        # 流式执行分析
        with st.chat_message("assistant"):
            status_placeholder = st.empty()
            report_placeholder = st.empty()
            chart_placeholder = st.empty()
            export_placeholder = st.empty()

            accumulated_report = ""
            all_charts = []
            all_exports = []
            final_result = None
            error_occurred = False

            # 使用 st.status 展示持久化的转圈动画 + 状态文本
            with st.status("🤔 AI 正在分析您的问题...", expanded=True) as analysis_status:
                try:
                    stream = st.session_state["analyst"].run_stream({
                        "query": prompt,
                        "history": history,
                    })

                    for event_type, data in stream:
                        if event_type == "status":
                            analysis_status.update(label=f"🔄 {data}")

                        elif event_type == "step_start":
                            step_info = data
                            analysis_status.update(label=f"⚙️ 步骤 {step_info['step']}: {step_info['task']}")
                            st.write(f"▶️ 开始: {step_info['task']}")

                        elif event_type == "step_done":
                            st.write(f"✅ 完成: 步骤 {data.get('step', '')}")

                        elif event_type == "report":
                            accumulated_report = data
                            # 报告生成完成后更新状态
                            analysis_status.update(label="📝 正在生成报告...", state="running")
                            with report_placeholder.container():
                                st.markdown(accumulated_report)
                                render_report_download(
                                    accumulated_report,
                                    f"download_report_{len(st.session_state['message'])}",
                                )

                        elif event_type == "charts":
                            all_charts = data
                            if all_charts:
                                analysis_status.update(label="📈 正在渲染图表...", state="running")
                                with chart_placeholder.container():
                                    st.divider()
                                    st.subheader("📈 生成的图表")
                                    cols = st.columns(min(len(all_charts), 2))
                                    for i, chart in enumerate(all_charts):
                                        chart_path = chart.get("path", "")
                                        if chart_path and os.path.exists(chart_path) and chart_path.endswith(".html"):
                                            with cols[i % 2]:
                                                st.caption(chart.get("title", ""))
                                                try:
                                                    with open(chart_path, "r", encoding="utf-8") as f:
                                                        st.components.v1.html(f.read(), height=450, scrolling=False)
                                                except Exception:
                                                    st.info(f"图表已保存至: {chart_path}")

                        elif event_type == "exports":
                            all_exports = data
                            if all_exports:
                                analysis_status.update(label="📥 准备下载文件...", state="running")
                                with export_placeholder.container():
                                    st.divider()
                                    st.subheader("📥 下载报告")
                                    for f in all_exports:
                                        fpath = f.get("path", "")
                                        ffmt = f.get("format", "")
                                        if fpath and os.path.exists(fpath):
                                            with open(fpath, "rb") as fb:
                                                st.download_button(
                                                    label=f"下载 {ffmt.upper()} 报告",
                                                    data=fb.read(),
                                                    file_name=os.path.basename(fpath),
                                                    mime={
                                                        "md": "text/markdown",
                                                        "html": "text/html",
                                                        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                                        "pdf": "application/pdf",
                                                    }.get(ffmt, "application/octet-stream"),
                                                    key=f"dl_{ffmt}_{int(time.time())}",
                                                )

                        elif event_type == "done":
                            final_result = data
                            if final_result.get("success", False):
                                analysis_status.update(label="✅ 分析完成！", state="complete", expanded=False)
                            else:
                                analysis_status.update(label="⚠️ 分析部分完成", state="complete", expanded=False)

                            # 记录助手回复到短期记忆
                            report_md = accumulated_report or "数据分析完成"
                            mem.add_assistant_message(report_md)

                        elif event_type == "error":
                            st.error(f"❌ {data}")
                            analysis_status.update(label="❌ 分析出错", state="error")
                            error_occurred = True

                except Exception as e:
                    import traceback
                    error_msg = f"数据分析过程中出现错误: {str(e)}"
                    st.error(error_msg)
                    st.code(traceback.format_exc())
                    analysis_status.update(label="❌ 分析出错", state="error")
                    error_occurred = True
                    final_result = {"success": False, "errors": [str(e)]}

            # 保存到消息历史
            st.session_state["message"].append({
                "role": "assistant",
                "content": accumulated_report,
                "mode": "数据分析",
                "analysis_result": final_result or {},
            })
