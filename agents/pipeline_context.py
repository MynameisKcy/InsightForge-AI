"""
PipelineContext: 类型化管道数据流，替代 prev_results 字典抓取袋。

每个代理阶段通过类型化字段读写，而非通过无类型的 dict。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PipelineContext:
    """管道上下文 —— 代理间数据流的唯一真实来源。

    PlannerAgent 创建实例，每个处理程序写入其输出槽位，后续处理程序
    从类型化字段读取。run_stream 在管道完成后读取最终结果。
    """

    # ── 核心数据载体 ──
    dataframe_json: str = ""

    # ── 代理输出槽位 ──
    sql_result: Optional[dict] = None
    trend_result: Optional[dict] = None
    product_result: Optional[dict] = None
    risk_result: Optional[dict] = None
    visualization_result: Optional[dict] = None
    report_result: Optional[dict] = None
    export_result: Optional[dict] = None

    # ── 元数据 ──
    title: str = ""
    errors: list[str] = field(default_factory=list)
    completed_steps: set[int] = field(default_factory=set)

    # ── 共享方法 ──

    @property
    def success(self) -> bool:
        return len(self.errors) == 0

    @property
    def charts(self) -> list:
        """从 visualization_result 中提取图表列表，方便 run_stream 读取。"""
        if self.visualization_result:
            return self.visualization_result.get("charts", [])
        return []

    @property
    def report_markdown(self) -> str:
        """从 report_result 中提取 markdown，方便 run_stream 读取。"""
        if self.report_result:
            return self.report_result.get("markdown", "")
        return ""

    @property
    def export_files(self) -> list:
        """从 export_result 中提取文件列表，方便 run_stream 读取。"""
        if self.export_result:
            return self.export_result.get("files", [])
        return []