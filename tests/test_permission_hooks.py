"""Permission hooks（#4，P2）：总线语义 + 默认 hook 委托现有 safety 函数。

离线测试：真实 database.safety 委托（行为恒等验证），不触达网络/DB。
"""
import os
import tempfile
import unittest

from utils.permission_hooks import (
    POINT_CSV_LOAD,
    POINT_FILE_WRITE,
    POINT_SQL_EXECUTE,
    POINT_TOOL_INVOKE,
    clear_hooks,
    register_hook,
    trigger_hooks,
)


class HookBusTests(unittest.TestCase):
    def setUp(self):
        clear_hooks(None)
        self.addCleanup(lambda: clear_hooks(None))

    def test_custom_point_first_blocking_wins(self):
        register_hook("test.point", lambda x: None if x < 3 else "too big")
        register_hook("test.point", lambda x: "never reached if first blocks")
        # 第一个 hook 放行 → 落到第二个（其恒拦截）
        self.assertEqual(trigger_hooks("test.point", x=1), "never reached if first blocks")
        # 第一个 hook 拦截 → 短路，第二个不执行
        self.assertEqual(trigger_hooks("test.point", x=5), "too big")

    def test_unregistered_point_passes(self):
        self.assertIsNone(trigger_hooks("no.such.point", anything=1))

    def test_hook_exception_fail_closed(self):
        def bad(x):
            raise ValueError("hook broke")

        register_hook("test.point", bad)
        reason = trigger_hooks("test.point", x=1)
        self.assertIsNotNone(reason)
        self.assertIn("hook 异常", reason)

    def test_clear_specific_point(self):
        register_hook("test.point", lambda x: "nope")
        clear_hooks("test.point")
        self.assertIsNone(trigger_hooks("test.point", x=1))


class DefaultHookTests(unittest.TestCase):
    """默认 hook 委托 database.safety 现有函数，行为恒等。"""

    def setUp(self):
        clear_hooks(None)
        self.addCleanup(lambda: clear_hooks(None))

    def test_sql_execute_blocks_write_statement(self):
        reason = trigger_hooks(POINT_SQL_EXECUTE, sql="DROP TABLE x", user_id="u1")
        self.assertIsNotNone(reason)
        self.assertIn("只读沙箱", reason)

    def test_sql_execute_allows_select(self):
        self.assertIsNone(trigger_hooks(POINT_SQL_EXECUTE, sql="SELECT 1", user_id="u1"))

    def test_sql_execute_blocks_file_function_in_select(self):
        reason = trigger_hooks(POINT_SQL_EXECUTE,
                               sql="SELECT * FROM read_csv_auto('/etc/passwd')")
        self.assertIsNotNone(reason)

    def test_csv_load_rejects_traversal(self):
        reason = trigger_hooks(POINT_CSV_LOAD, path="../evil.csv")
        self.assertIsNotNone(reason)

    def test_csv_load_rejects_outside_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "x.csv")
            reason = trigger_hooks(POINT_CSV_LOAD, path=outside)
        self.assertIsNotNone(reason)

    def test_file_write_allows_data_subdir(self):
        from utils.path_tool import get_abs_path
        p = os.path.join(get_abs_path("data/tasks"), "u1", "task_x.json")
        self.assertIsNone(trigger_hooks(POINT_FILE_WRITE, path=p, purpose="task"))

    def test_file_write_rejects_outside_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            reason = trigger_hooks(POINT_FILE_WRITE, path=os.path.join(tmp, "x.txt"))
        self.assertIsNotNone(reason)
        self.assertIn("越界", reason)

    def test_tool_invoke_audit_only_passes(self):
        self.assertIsNone(trigger_hooks(POINT_TOOL_INVOKE,
                                        tool_name="run_full_analysis", args={}))


class ToolInvokeMiddlewareTests(unittest.TestCase):
    """tool.invoke 拦截点端到端：monitor_tool 接线（默认恒放行；注册规则即拦截）。"""

    def setUp(self):
        clear_hooks(None)
        self.addCleanup(lambda: clear_hooks(None))

    @staticmethod
    def _req():
        from langchain.tools.tool_node import ToolCallRequest
        return ToolCallRequest(
            tool_call={"name": "run_full_analysis", "args": {"query": "x"},
                       "id": "call_1"},
            tool=None, state=None, runtime=None)

    @staticmethod
    def _patch_decision():
        from unittest.mock import patch
        return patch("agent.tools.middleware._log_tool_decision")

    def test_blocking_hook_returns_permission_tool_message(self):
        from langchain_core.messages import ToolMessage

        from agent.tools.middleware import monitor_tool

        register_hook(POINT_TOOL_INVOKE, lambda **kw: "工具被策略拒绝")
        with self._patch_decision():
            result = monitor_tool.wrap_tool_call(
                self._req(),
                lambda r: ToolMessage(content="should not run", name="x",
                                      tool_call_id="call_1"))
        self.assertIsInstance(result, ToolMessage)
        self.assertIn("权限拦截", result.content)
        self.assertIn("拒绝", result.content)

    def test_default_passes_through_to_handler(self):
        from langchain_core.messages import ToolMessage

        from agent.tools.middleware import monitor_tool

        called = []

        def handler(req):
            called.append(req)
            return ToolMessage(content="ok", name="run_full_analysis",
                               tool_call_id="call_1")

        with self._patch_decision():
            result = monitor_tool.wrap_tool_call(self._req(), handler)
        self.assertEqual(result.content, "ok")
        self.assertEqual(len(called), 1)   # 默认 hook 恒放行，handler 正常执行

    # ── 模式副作用（ADR-0004 扩展：目录表 mode_effect 替代名字魔法串）──

    def _req_with_runtime(self, tool_name: str, context: dict):
        """带 runtime.context 的请求：effect 置位需要 context dict。"""
        from types import SimpleNamespace

        from langchain.tools.tool_node import ToolCallRequest
        return ToolCallRequest(
            tool_call={"name": tool_name, "args": {}, "id": "call_1"},
            tool=None, state=None,
            runtime=SimpleNamespace(context=context))

    def test_mode_effect_sets_report_flag(self):
        """fill_report_context_for_report 命中目录表 effect -> context['report']=True。"""
        from langchain_core.messages import ToolMessage

        from agent.tools.middleware import monitor_tool

        context = {}
        req = self._req_with_runtime("fill_report_context_for_report", context)
        with self._patch_decision():
            monitor_tool.wrap_tool_call(
                req, lambda r: ToolMessage(content="ok", name="x",
                                           tool_call_id="call_1"))
        self.assertTrue(context.get("report"))

    def test_non_effect_tool_leaves_context_untouched(self):
        """普通工具无 mode_effect -> context 不被污染。"""
        from langchain_core.messages import ToolMessage

        from agent.tools.middleware import monitor_tool

        context = {"existing": 1}
        req = self._req_with_runtime("run_full_analysis", context)
        with self._patch_decision():
            monitor_tool.wrap_tool_call(
                req, lambda r: ToolMessage(content="ok", name="x",
                                           tool_call_id="call_1"))
        self.assertEqual(context, {"existing": 1})


if __name__ == "__main__":
    unittest.main()
