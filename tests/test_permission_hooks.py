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


if __name__ == "__main__":
    unittest.main()
