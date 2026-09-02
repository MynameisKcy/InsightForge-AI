"""#3 WorkflowRunner 执行边界：journal / 结果缓存 / 异常留痕。

离线测试：fake caller（不触达 LLM）。runner 的 journal/cache 引用外部传入，
验证「请求作用域共享」契约——三个 AnalysisAgent 并发写同一 journal。
"""
import unittest

from agents.pipeline_context import PipelineContext
from agents.workflow import WorkflowRunner


class _FakeCaller:
    """记录 _call_llm_with_schema 调用次数，可配置返回值/异常。"""

    def __init__(self, result=None, exc=None):
        self.calls = 0
        self.result = result
        self.exc = exc

    def _call_llm_with_schema(self, messages, schema):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


class WorkflowRunnerTests(unittest.TestCase):
    def setUp(self):
        self.pctx = PipelineContext()
        self.runner = WorkflowRunner(self.pctx.journal, self.pctx.stage_cache)

    def test_ok_path_journals_and_caches(self):
        caller = _FakeCaller(result={"insight": "x"})
        out = self.runner.agent(caller, [{"role": "user", "content": "q"}],
                                {"type": "object"}, label="insight.X", phase="Analyze")
        self.assertEqual(out, {"insight": "x"})
        self.assertEqual(len(self.pctx.journal), 1)
        e = self.pctx.journal[0]
        self.assertEqual(e["label"], "insight.X")
        self.assertEqual(e["phase"], "Analyze")
        self.assertEqual(e["status"], "ok")
        self.assertIsNone(e["error"])
        self.assertIsInstance(e["duration_ms"], float)

    def test_cache_hit_skips_second_llm_call(self):
        caller = _FakeCaller(result={"insight": "x"})
        msgs = [{"role": "user", "content": "same prompt"}]
        self.runner.agent(caller, msgs, {}, label="insight.X")
        self.runner.agent(caller, msgs, {}, label="insight.X")
        self.assertEqual(caller.calls, 1)          # 第二次命中缓存
        self.assertEqual(self.pctx.journal[-1]["status"], "cache_hit")

    def test_failure_not_cached(self):
        caller = _FakeCaller(result=None)
        msgs = [{"role": "user", "content": "bad"}]
        out = self.runner.agent(caller, msgs, {}, label="insight.X")
        self.assertIsNone(out)
        self.assertEqual(self.pctx.journal[0]["status"], "failed")
        # 失败不写缓存：相同输入下次仍走 LLM（允许后续成功）
        self.runner.agent(caller, msgs, {}, label="insight.X")
        self.assertEqual(caller.calls, 2)

    def test_exception_journals_then_reraises(self):
        caller = _FakeCaller(exc=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self.runner.agent(caller, [{"role": "user", "content": "q"}], {},
                              label="insight.X")
        e = self.pctx.journal[0]
        self.assertEqual(e["status"], "error")
        self.assertIn("boom", e["error"])

    def test_journal_shared_across_runners(self):
        """同一 pctx 上的两个 runner 共享一份 journal（并发组语义）。"""
        r2 = WorkflowRunner(self.pctx.journal, self.pctx.stage_cache)
        self.runner.agent(_FakeCaller(result={"a": 1}), [{"role": "user", "content": "1"}], {}, label="A")
        r2.agent(_FakeCaller(result={"b": 2}), [{"role": "user", "content": "2"}], {}, label="B")
        self.assertEqual(len(self.pctx.journal), 2)
        self.assertEqual([e["label"] for e in self.pctx.journal], ["A", "B"])

    def test_standalone_runner_owns_local_state(self):
        """无 pctx 独立使用：自建 journal/cache，互不污染。"""
        r1, r2 = WorkflowRunner(), WorkflowRunner()
        r1.agent(_FakeCaller(result={}), [{"role": "user", "content": "q"}], {}, label="A")
        self.assertEqual(len(r1.journal), 1)
        self.assertEqual(len(r2.journal), 0)


if __name__ == "__main__":
    unittest.main()
