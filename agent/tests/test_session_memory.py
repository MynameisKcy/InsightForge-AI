"""Session Memory 单测 (ADR-0003 Phase 1)。

覆盖 Phase 1 三项目标:
1. 修泄漏 —— 同一 user 的 A/B session 互不串(池按 session_id key)。
2. 修不回灌 —— 池 miss 时从 conversation_history(turn_index > 水印)+ chat_sessions.summary 回灌。
3. 水印模型 —— 超过 MAX_TURNS 时折叠最老若干轮并入滚动摘要,推进 summarized_up_to,
   工作窗口只保留水印后的轮次,摘要 + 水印持久化到 chat_sessions。

外加:LRU 淘汰、clear_session、空 session_id 降级。

依赖注入:打桩 short_term._get_ltm(临时 SQLite 文件,跨连接共享)与
short_term._get_summarizer(伪摘要器),避免触达真实 DashScope LLM 与生产 memory.db。
"""
import os
import sys
import tempfile
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from memory.short_term import get_session, clear_session, ConversationMemory, MAX_TURNS
from memory.long_term import LongTermMemory
import memory.short_term as short_term_mod


class _FakeSummarizer:
    """伪摘要器:summarize 恒返回固定文本,记录调用次数与入参轮数。"""

    def __init__(self, text="FAKE_SUMMARY"):
        self.text = text
        self.calls = []  # 每次调用的 fold 轮数(按 user 消息计)

    def summarize(self, turns, previous_summary=""):
        self.calls.append(sum(1 for t in turns if t.get("role") == "user"))
        return self.text


class SessionMemoryTests(unittest.TestCase):
    def setUp(self):
        # 隔离的临时 DB(文件,跨连接共享;:memory: 每次连接新建会丢 schema)
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)  # 让 LongTermMemory 自行创建
        self.ltm = LongTermMemory(db_path=self.db_path)

        # 注入测试依赖(整段替换,绕开内部 import,规避双模块陷阱)
        self._orig_get_ltm = short_term_mod._get_ltm
        self._orig_get_summarizer = short_term_mod._get_summarizer
        self._fake_summarizer = _FakeSummarizer()
        short_term_mod._get_ltm = lambda: self.ltm
        short_term_mod._get_summarizer = lambda: self._fake_summarizer

        # 清空全局池,隔离用例
        short_term_mod._session_pool.clear()

    def tearDown(self):
        short_term_mod._get_ltm = self._orig_get_ltm
        short_term_mod._get_summarizer = self._orig_get_summarizer
        short_term_mod._session_pool.clear()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # ── 1. 修泄漏:同 user 的 A/B session 不串 ──
    def test_same_user_sessions_are_isolated(self):
        mem_a = get_session("sessA", "u1")
        mem_a.add_user_message("A 的问题")
        mem_a.add_assistant_message("A 的回答")

        mem_b = get_session("sessB", "u1")
        mem_b.add_user_message("B 的问题")
        mem_b.add_assistant_message("B 的回答")

        ctx_a = mem_a.get_context()
        ctx_b = mem_b.get_context()

        contents_a = " ".join(m["content"] for m in ctx_a)
        contents_b = " ".join(m["content"] for m in ctx_b)
        self.assertIn("A 的问题", contents_a)
        self.assertNotIn("B 的问题", contents_a)
        self.assertIn("B 的问题", contents_b)
        self.assertNotIn("A 的问题", contents_b)

    # ── 2. 修不回灌:池 miss 后从 DB 回灌 ──
    def test_hydrate_from_db_on_pool_miss(self):
        sid = self.ltm.create_session("u1", title="t")
        # 写入 3 轮(turn_index 0/1/2)
        for i in range(3):
            self.ltm.save_conversation_pair("u1", f"q{i}", f"a{i}", session_id=sid)
        # 设滚动摘要 + 水印:turn_index 0 已折叠进摘要,工作窗口应为 turn_index 1/2
        self.ltm.save_session_memory_meta(sid, "已折叠的摘要", 0)

        # 模拟重启:池被清空后重新取
        short_term_mod._session_pool.clear()
        mem = get_session(sid, "u1")
        ctx = mem.get_context()  # 触发回灌

        self.assertEqual(mem.summary, "已折叠的摘要")
        self.assertEqual(mem.summarized_up_to, 0)
        contents = " ".join(m["content"] for m in ctx)
        self.assertNotIn("q0", contents)  # 水印内,已折叠
        self.assertIn("q1", contents)  # 水印后,回灌
        self.assertIn("q2", contents)

    def test_hydrate_empty_session_no_summary(self):
        """全新会话(无轮次、无摘要):回灌得空,不报错。"""
        sid = self.ltm.create_session("u1", title="t")
        short_term_mod._session_pool.clear()
        mem = get_session(sid, "u1")
        ctx = mem.get_context()
        self.assertEqual(mem.summary, "")
        self.assertEqual(mem.summarized_up_to, -1)
        self.assertEqual(ctx, [])

    # ── 3. token 触发压缩（实测 input_tokens >= 90%）──
    def test_compress_on_real_token_measurement(self):
        sid = self.ltm.create_session("u1", title="t")
        mem = get_session(sid, "u1")
        mem._context_window = 1000            # 小窗口便于触发
        mem.last_measured_input_tokens = 950  # >= 90% * 1000
        for i in range(8):                    # 足够折叠（size > min_keep）
            mem.add_user_message(f"q{i}")
            mem.add_assistant_message(f"a{i}")

        self.assertTrue(self._fake_summarizer.calls, "实测 token 达阈值应触发压缩")
        self.assertEqual(mem.summary, "FAKE_SUMMARY")
        self.assertGreaterEqual(mem.summarized_up_to, 0)  # 水印推进
        self.assertLess(mem.size(), 8)  # 折半折叠后工作窗口有界
        contents = " ".join(m["content"] for m in mem.get_context())
        self.assertIn("q7", contents)     # 最新轮保留
        self.assertNotIn("q0", contents)  # 最老轮已折叠进摘要
        meta = self.ltm.get_session_memory_meta(sid)
        self.assertIsNotNone(meta)
        self.assertEqual(meta[0], "FAKE_SUMMARY")

    def test_no_compress_below_threshold(self):
        sid = self.ltm.create_session("u1", title="t")
        mem = get_session(sid, "u1")
        mem._context_window = 1000
        mem.last_measured_input_tokens = 500  # 50% < 90%,不触发
        for i in range(8):
            mem.add_user_message(f"q{i}")
            mem.add_assistant_message(f"a{i}")
        self.assertFalse(self._fake_summarizer.calls)
        self.assertEqual(mem.summary, "")
        self.assertEqual(mem.summarized_up_to, -1)

    # ── 4. 字符兜底触发（无实测 token, chars >= 80%）──
    def test_compress_on_char_fallback(self):
        sid = self.ltm.create_session("u1", title="t")
        mem = get_session(sid, "u1")
        mem._context_window = 100  # 80% = 80 token ≈ 200 chars（2.5 chars/token）
        # 无 last_measured_input_tokens -> 走字符兜底
        for i in range(8):
            mem.add_user_message(f"问题内容编号{i}的销售数据趋势分析报告")
            mem.add_assistant_message(f"回答内容编号{i}的销售数据趋势结果汇总")
        self.assertTrue(self._fake_summarizer.calls, "字符估算达 80% 应触发兜底压缩")
        self.assertEqual(mem.summary, "FAKE_SUMMARY")

    # ── 5. 压缩后实测值失效（下一轮退回字符兜底,直至新一轮模型调用回填）──
    def test_measurement_invalidated_after_compress(self):
        sid = self.ltm.create_session("u1", title="t")
        mem = get_session(sid, "u1")
        mem._context_window = 1000
        mem.last_measured_input_tokens = 950
        for i in range(8):
            mem.add_user_message(f"q{i}")
            mem.add_assistant_message(f"a{i}")
        self.assertIsNone(mem.last_measured_input_tokens)

    # ── 6. get_context(max_turns=None) 不截断（聊天路径依赖 token 预算,非轮数帽）──
    def test_get_context_no_cap(self):
        mem = get_session("sNC", "u1")
        mem._context_window = 100000  # 大窗口,不触发压缩
        for i in range(5):
            mem.add_user_message(f"q{i}")
            mem.add_assistant_message(f"a{i}")
        ctx = mem.get_context(max_turns=None)
        self.assertEqual(len(ctx), 10)  # 5 轮 = 10 条,全部返回

    def test_record_input_tokens(self):
        mem = get_session("sRT", "u1")
        mem.record_input_tokens(1234)
        self.assertEqual(mem.last_measured_input_tokens, 1234)
        mem.record_input_tokens(None)  # None 不覆盖
        self.assertEqual(mem.last_measured_input_tokens, 1234)

    # ── 4. LRU 淘汰 ──
    def test_lru_evicts_oldest_and_promotes_on_access(self):
        cap = short_term_mod.SESSION_POOL_CAP
        # 填满到上限
        for i in range(cap):
            get_session(f"s{i}", "u1")
        self.assertEqual(len(short_term_mod._session_pool), cap)
        # 访问最老的 s0,提升为最近使用
        get_session("s0", "u1")
        # 再加 1 个,应淘汰当前最久未用(s1)
        get_session("s_new", "u1")
        self.assertEqual(len(short_term_mod._session_pool), cap)
        self.assertIn("s0", short_term_mod._session_pool)  # 被访问过,保留
        self.assertNotIn("s1", short_term_mod._session_pool)  # 最久未用,淘汰
        self.assertIn("s_new", short_term_mod._session_pool)

    # ── 5. clear_session ──
    def test_clear_session_removes_from_pool(self):
        mem = get_session("sX", "u1")
        self.assertIn("sX", short_term_mod._session_pool)
        clear_session("sX")
        self.assertNotIn("sX", short_term_mod._session_pool)
        # 再次取应得新实例
        mem2 = get_session("sX", "u1")
        self.assertIsNot(mem, mem2)

    # ── 降级:空 session_id(/api/analysis 退役路径)不回灌、不报错 ──
    def test_empty_session_id_works_without_hydration(self):
        mem = get_session("", "u1")
        mem.add_user_message("x")
        mem.add_assistant_message("y")
        ctx = mem.get_context()
        self.assertEqual(mem.summary, "")
        self.assertEqual(mem.summarized_up_to, -1)
        contents = " ".join(m["content"] for m in ctx)
        self.assertIn("x", contents)
        self.assertIn("y", contents)


if __name__ == "__main__":
    unittest.main()
