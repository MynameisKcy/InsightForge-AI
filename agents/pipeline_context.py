"""
PipelineContext: 类型化管道数据流，替代 prev_results 字典抓取袋。

每个代理阶段通过类型化字段读写，而非通过无类型的 dict。
"""

from dataclasses import dataclass, field


@dataclass
class PipelineContext:
    """管道上下文 —— 代理间数据流的唯一真实来源。

    PlannerAgent 创建实例，每个处理程序写入其输出槽位，后续处理程序
    从类型化字段读取。run_stream 在管道完成后读取最终结果。
    """

    # ── 核心数据载体 ──
    dataframe_json: str = ""

    # ── 代理输出槽位 ──
    sql_result: dict | None = None
    trend_result: dict | None = None
    product_result: dict | None = None
    risk_result: dict | None = None
    visualization_result: dict | None = None
    report_result: dict | None = None
    export_result: dict | None = None

    # ── 元数据 ──
    title: str = ""
    errors: list[str] = field(default_factory=list)
    completed_steps: set[int] = field(default_factory=set)

    # ── WorkflowRunner 执行边界（#3）：请求级 journal 与结果缓存 ──
    # journal: 每次 LLM 执行边界调用的记录 [{label, phase, status, duration_ms, error, at}]，
    #          P1 TaskRecord 持久化的数据源；stage_cache: 请求内结果缓存
    #          （key = label + prompt 哈希，同输入不重跑 LLM）。均为请求作用域，
    #          禁止提升为模块/实例全局（多用户隔离红线）。
    journal: list[dict] = field(default_factory=list)
    stage_cache: dict = field(default_factory=dict)

    # ── 阶段超时弃用标记（孤儿图表治理） ──
    # 超时语义是"放弃线程"：被弃线程仍在跑，其 LLM 稍后返回会继续生成图表。
    # 落库方（visualization）据此实时跳过持久化，避免用户已收到报错、图表
    # 却迟到出现在知识库/结果里。只增不减，set.add 线程安全（GIL）。
    abandoned_agents: set[str] = field(default_factory=set)

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