"""Long-Term Memory 跨会话召回单测 (ADR-0003 Phase 3)。

覆盖 Phase 3 四项目标:
1. 召回库隔离 -- A 的终版摘要 B 检索不到(owner 过滤)。
2. 会话结束写终版摘要 -- finalize_session 合并(滚动摘要 + 剩余轮次)压一份,
   upsert 进 memory collection + 同步 memory_summaries + 标记 finalized_up_to。
3. 召回 + 注入格式 -- recall 返回 "## 历史会话记忆" 节,带标题;可排除当前会话。
4. 清理 + 闲置检测 -- 删会话清 embedding;闲置会话被 finalize,未闲置/已 finalize 跳过,
   有新轮次则重新 finalize。

依赖注入:真实 LongTermMemory(临时 SQLite 文件) + 假 embed Chroma(memory collection)
+ 伪摘要器,避免触达真实 DashScope LLM / embed / 生产库。

rerank 够不到 DashScope:用例保持候选数 <= top_n,使 recall 跳过 rerank 走粗召回,
不发起网络调用(与 rag_service._rerank 的 `len(docs) <= top_n` 早退一致)。
"""
import os
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from memory.long_term import LongTermMemory
from rag.vector_store import VectorStoreService


class _FakeEmbed(Embeddings):
    """确定性假嵌入,不依赖 DashScope。"""

    def embed_documents(self, texts):
        return [[float(len(t) % 7), float((hash(t) & 0xFFFF) % 13)] for t in texts]

    def embed_query(self, text):
        return [float(len(text) % 7), float((hash(text) & 0xFFFF) % 13)]


class _FakeSummarizer:
    """伪摘要器:把 previous_summary + 各轮内容拼成确定性摘要,便于断言。"""

    def summarize(self, turns, previous_summary=""):
        parts = [previous_summary] if previous_summary else []
        for t in turns:
            parts.append(f"{t.get('role')}:{t.get('content')}")
        return " | ".join(parts).strip() or "EMPTY"


def _make_memory_store() -> VectorStoreService:
    """构造未初始化的 VectorStoreService,注入 in-memory Chroma(memory collection)。

    跳过真实 embed、persist 与 legacy owner 迁移;每次用唯一 collection 名隔离用例。
    __new__ 跳过 __init__,需手动设 _lock(Fix C:并发串行化锁)。
    """
    vs = VectorStoreService.__new__(VectorStoreService)
    vs.collection_name = "memory"
    vs.vector_store = Chroma(
        collection_name=f"mem_test_{uuid.uuid4().hex[:8]}",
        embedding_function=_FakeEmbed(),
    )
    vs.spliter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    vs._lock = threading.Lock()
    return vs


class MemoryRecallTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.ltm = LongTermMemory(db_path=self.db_path)
        self.memory_store = _make_memory_store()
        # 延迟导入:recall 模块尚未实现时此处会 ImportError,正是红阶段信号
        from memory.recall import MemoryRecallService
        self.recall = MemoryRecallService(
            ltm=self.ltm,
            memory_store=self.memory_store,
            summarizer=_FakeSummarizer(),
        )

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _backdate(self, sid, hours_ago):
        """把会话 updated_at 倒拨指定小时,模拟闲置。"""
        old = (datetime.now() - timedelta(hours=hours_ago)).isoformat()
        with self.ltm._get_conn() as conn:
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                (old, sid),
            )
            conn.commit()

    # ── 1. finalize 写终版摘要(滚动摘要 + 剩余轮次) ──
    def test_finalize_writes_to_collection_and_db(self):
        sid = self.ltm.create_session("alice", title="销售分析")
        self.ltm.save_conversation_pair("alice", "查山东销售", "山东销售100万", session_id=sid)
        # 设滚动摘要 + 水印:turn 0 已折叠,工作窗口为空 -> 终版摘要即滚动摘要
        self.ltm.save_session_memory_meta(sid, "滚动摘要:销售", 0)

        final = self.recall.finalize_session(sid, "alice")

        self.assertIn("滚动摘要:销售", final)
        # memory collection 有该会话的 embedding
        hits = self.memory_store.retrieve_session_memories("销售", "alice", k=10)
        self.assertTrue(any(d.metadata.get("session_id") == sid for d in hits))
        # memory_summaries 留有审计行
        rows = self.ltm.get_recent_summaries("alice", limit=10)
        self.assertTrue(any(r["summary"] == final and r.get("session_id") == sid for r in rows))
        # finalized_up_to 推进到最新 turn_index
        self.assertEqual(self.ltm.get_session_max_turn_index(sid), 0)
        # finalize 后标记应覆盖到最新轮
        with self.ltm._get_conn() as conn:
            row = conn.execute(
                "SELECT finalized_up_to FROM chat_sessions WHERE session_id = ?", (sid,)
            ).fetchone()
        self.assertEqual(row["finalized_up_to"], 0)

    def test_finalize_merges_rolling_summary_and_remaining_turns(self):
        """有剩余轮次时,终版摘要 = 合并(滚动摘要 + 剩余水印后轮次)。"""
        sid = self.ltm.create_session("alice", title="t")
        self.ltm.save_conversation_pair("alice", "q0", "a0", session_id=sid)  # turn 0
        self.ltm.save_conversation_pair("alice", "q1", "a1", session_id=sid)  # turn 1
        # turn 0 折叠进滚动摘要,工作窗口剩 turn 1
        self.ltm.save_session_memory_meta(sid, "PREV", 0)

        final = self.recall.finalize_session(sid, "alice")

        self.assertIn("PREV", final)            # 滚动摘要并入
        self.assertIn("q1", final)              # 剩余轮次并入
        self.assertNotIn("q0", final)           # 水印内轮次不重复(已在 PREV 里)

    # ── 2. upsert:重复 finalize 覆盖旧 embedding(同 session_id 仅一份) ──
    def test_finalize_upsert_overwrites(self):
        sid = self.ltm.create_session("alice", title="t")
        self.ltm.save_conversation_pair("alice", "q0", "a0", session_id=sid)

        self.recall.finalize_session(sid, "alice")
        # 新增一轮后再次 finalize
        self.ltm.save_conversation_pair("alice", "q1", "a1", session_id=sid)
        final2 = self.recall.finalize_session(sid, "alice")

        hits = self.memory_store.retrieve_session_memories("q", "alice", k=10)
        sids = [d.metadata.get("session_id") for d in hits]
        self.assertEqual(sids.count(sid), 1, "同 session_id 仅一份 embedding")
        self.assertIn("q1", final2)

    # ── 3. 召回 owner 隔离:A 召不到 B ──
    def test_recall_isolated_by_owner(self):
        sid_a = self.ltm.create_session("alice", title="销售")
        self.ltm.save_conversation_pair("alice", "查销售", "销售100万", session_id=sid_a)
        self.recall.finalize_session(sid_a, "alice")

        sid_b = self.ltm.create_session("bob", title="库存")
        self.ltm.save_conversation_pair("bob", "查库存", "库存50件", session_id=sid_b)
        self.recall.finalize_session(sid_b, "bob")

        alice = self.recall.recall("销售", "alice", top_n=3, coarse_k=5)
        bob = self.recall.recall("库存", "bob", top_n=3, coarse_k=5)
        self.assertIn("销售", alice)
        self.assertNotIn("库存", alice)
        self.assertIn("库存", bob)
        self.assertNotIn("销售", bob)

    # ── 4. recall 返回格式化 "## 历史会话记忆" 节 ──
    def test_recall_returns_formatted_section(self):
        sid = self.ltm.create_session("alice", title="销售分析")
        self.ltm.save_conversation_pair("alice", "查销售", "销售100万", session_id=sid)
        self.recall.finalize_session(sid, "alice")

        text = self.recall.recall("销售", "alice", top_n=3, coarse_k=5)
        self.assertIn("## 历史会话记忆", text)
        self.assertIn("销售分析", text)  # 带标题

    def test_recall_empty_when_no_memories(self):
        self.assertEqual(self.recall.recall("任意", "alice", top_n=3, coarse_k=5), "")

    # ── 5. recall 排除当前会话(避免召回自己刚 finalize 的摘要) ──
    def test_recall_excludes_current_session(self):
        sid_a = self.ltm.create_session("alice", title="销售")
        self.ltm.save_conversation_pair("alice", "查销售", "销售100万", session_id=sid_a)
        self.recall.finalize_session(sid_a, "alice")
        sid_b = self.ltm.create_session("alice", title="库存")
        self.ltm.save_conversation_pair("alice", "查库存", "库存50件", session_id=sid_b)
        self.recall.finalize_session(sid_b, "alice")

        # 在 sid_a 会话中召回,排除 sid_a -> 只剩 sid_b
        text = self.recall.recall("数据", "alice", top_n=3, coarse_k=5, exclude_session_id=sid_a)
        self.assertNotIn("销售100万", text)
        self.assertIn("库存50件", text)

    # ── 6. 删会话清 embedding ──
    def test_delete_session_cleans_embedding(self):
        sid = self.ltm.create_session("alice", title="t")
        self.ltm.save_conversation_pair("alice", "查销售", "销售100万", session_id=sid)
        self.recall.finalize_session(sid, "alice")
        self.assertTrue(self.memory_store.retrieve_session_memories("销售", "alice", k=10))

        self.memory_store.delete_session_memory(sid, "alice")

        self.assertFalse(self.memory_store.retrieve_session_memories("销售", "alice", k=10))

    # ── 7. 闲置检测:闲置会话被 finalize,未闲置跳过 ──
    def test_finalize_idle_sessions_finalizes_idle_only(self):
        sid_idle = self.ltm.create_session("alice", title="闲置")
        self.ltm.save_conversation_pair("alice", "q", "a", session_id=sid_idle)
        self._backdate(sid_idle, hours_ago=2)  # 闲置 2h

        sid_recent = self.ltm.create_session("alice", title="最近")
        self.ltm.save_conversation_pair("alice", "q2", "a2", session_id=sid_recent)
        # 不倒拨 -> 未闲置

        finalized = self.recall.finalize_idle_sessions("alice", except_session_id="",
                                                        idle_seconds=3600)
        self.assertIn(sid_idle, finalized)
        self.assertNotIn(sid_recent, finalized)
        # 闲置会话已写入 memory collection
        self.assertTrue(self.memory_store.retrieve_session_memories("q", "alice", k=10))

    def test_finalize_idle_sessions_skips_already_finalized(self):
        sid = self.ltm.create_session("alice", title="t")
        self.ltm.save_conversation_pair("alice", "q0", "a0", session_id=sid)
        self.recall.finalize_session(sid, "alice")  # 已 finalize 到 turn 0
        self._backdate(sid, hours_ago=2)

        finalized = self.recall.finalize_idle_sessions("alice", except_session_id="",
                                                        idle_seconds=3600)
        self.assertNotIn(sid, finalized, "无新轮次,不应重复 finalize")

    def test_finalize_idle_sessions_refinalizes_on_new_turns(self):
        sid = self.ltm.create_session("alice", title="t")
        self.ltm.save_conversation_pair("alice", "q0", "a0", session_id=sid)
        self.recall.finalize_session(sid, "alice")  # finalized_up_to = 0
        # 新增一轮 -> max turn_index = 1 > finalized_up_to
        self.ltm.save_conversation_pair("alice", "q1", "a1", session_id=sid)
        self._backdate(sid, hours_ago=2)

        finalized = self.recall.finalize_idle_sessions("alice", except_session_id="",
                                                        idle_seconds=3600)
        self.assertIn(sid, finalized, "有新轮次,应重新 finalize")
        # memory collection 内容已更新(含 q1)
        hits = self.memory_store.retrieve_session_memories("q", "alice", k=10)
        self.assertTrue(any("q1" in d.page_content for d in hits))

    def test_finalize_idle_sessions_excludes_current(self):
        """当前会话即便闲置也不被 finalize(用户正在用它)。"""
        sid = self.ltm.create_session("alice", title="t")
        self.ltm.save_conversation_pair("alice", "q", "a", session_id=sid)
        self._backdate(sid, hours_ago=2)

        finalized = self.recall.finalize_idle_sessions("alice", except_session_id=sid,
                                                        idle_seconds=3600)
        self.assertNotIn(sid, finalized)

    def test_finalize_session_no_turns_returns_empty(self):
        """会话存在但无任何轮次:终版摘要为空,不写入。"""
        sid = self.ltm.create_session("alice", title="空")
        final = self.recall.finalize_session(sid, "alice")
        self.assertEqual(final, "")
        self.assertFalse(self.memory_store.retrieve_session_memories("x", "alice", k=10))

    def test_recall_section_capped(self):
        """召回节超 SECTION_CHAR_CAP 时截断（Fix B：约束字符兜底 token 估算偏差）。"""
        from memory.recall import SECTION_CHAR_CAP
        sid = self.ltm.create_session("alice", title="长摘要")
        self.ltm.save_conversation_pair("alice", "q", "a", session_id=sid)
        self.ltm.save_session_memory_meta(sid, "X" * (SECTION_CHAR_CAP + 800), -1)
        self.recall.finalize_session(sid, "alice")
        text = self.recall.recall("q", "alice", top_n=3, coarse_k=5)
        self.assertLessEqual(len(text), SECTION_CHAR_CAP + 1)  # +1 末尾 …
        self.assertTrue(text.endswith("…"))


class _FakeRuntime:
    def __init__(self, context=None):
        self.context = context if context is not None else {}


class _FakeModelRequest:
    """鸭子类型 ModelRequest：_build_system_prompt 只读 .messages / .runtime.context。"""

    def __init__(self, messages, runtime):
        self.messages = messages
        self.runtime = runtime


class MiddlewareRecallInjectionTests(unittest.TestCase):
    """召回注入移进 @dynamic_prompt 中间件（Fix A）：_build_system_prompt 统一管控。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.ltm = LongTermMemory(db_path=self.db_path)
        self.memory_store = _make_memory_store()
        from memory.recall import MemoryRecallService
        self.recall = MemoryRecallService(
            ltm=self.ltm, memory_store=self.memory_store, summarizer=_FakeSummarizer()
        )
        # session A（销售）终版摘要入库
        self.sid_a = self.ltm.create_session("alice", title="销售分析")
        self.ltm.save_conversation_pair("alice", "查山东销售", "山东销售100万", session_id=self.sid_a)
        self.recall.finalize_session(self.sid_a, "alice")
        # 设 contextvar（中间件 _recall_for_turn 经 get_user_id/get_session_id 读）
        from utils.request_context import set_request_context
        self._token = set_request_context(user_id="alice", session_id="sid_b")
        # patch get_memory_recall -> 测试 recall service
        self._patcher = patch("memory.recall.get_memory_recall", return_value=self.recall)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        from utils.request_context import reset_request_context
        reset_request_context(self._token)
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_normal_mode_appends_recall_e2e(self):
        """e2e：session A(销售)结束 -> B 问"对比上次销售" -> system prompt 含 A 摘要。"""
        from agent.tools.middleware import _build_system_prompt
        req = _FakeModelRequest(
            [{"role": "user", "content": "对比上次销售"}],
            _FakeRuntime({"report": False}),
        )
        prompt = _build_system_prompt(req)
        self.assertIn("## 历史会话记忆", prompt)
        self.assertIn("销售", prompt)  # 命中 A 摘要

    def test_report_mode_excludes_recall(self):
        """报告模式不注入历史会话记忆（Fix A #1：防泄漏）。"""
        from agent.tools.middleware import _build_system_prompt
        req = _FakeModelRequest(
            [{"role": "user", "content": "生成报告"}],
            _FakeRuntime({"report": True}),
        )
        prompt = _build_system_prompt(req)
        self.assertNotIn("## 历史会话记忆", prompt)

    def test_recall_cached_within_turn(self):
        """同轮多次模型调用：runtime.context 缓存，recall 只 embed 一次（Fix A 性能）。"""
        from agent.tools.middleware import _build_system_prompt
        calls = []
        original = self.recall.recall

        def counting(*a, **k):
            calls.append(1)
            return original(*a, **k)

        self.recall.recall = counting
        req = _FakeModelRequest(
            [{"role": "user", "content": "对比上次销售"}],
            _FakeRuntime({"report": False}),
        )
        _build_system_prompt(req)
        _build_system_prompt(req)  # 同 request -> 同 runtime.context -> 缓存命中
        self.assertEqual(len(calls), 1)

    def test_no_recall_when_no_user_context(self):
        """未设 contextvar（uid=default）时不召回，避免误用 default 用户记忆。"""
        from utils.request_context import reset_request_context, set_request_context
        tok = set_request_context(user_id="default", session_id="")
        try:
            from agent.tools.middleware import _build_system_prompt
            req = _FakeModelRequest(
                [{"role": "user", "content": "x"}],
                _FakeRuntime({"report": False}),
            )
            prompt = _build_system_prompt(req)
            self.assertNotIn("## 历史会话记忆", prompt)
        finally:
            reset_request_context(tok)


class MemoryRecallConcurrencyTests(unittest.TestCase):
    """memory collection 并发访问串行化（Fix C）：finalize 写 + recall 读 不崩不损坏。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.ltm = LongTermMemory(db_path=self.db_path)
        self.memory_store = _make_memory_store()
        from memory.recall import MemoryRecallService
        self.recall = MemoryRecallService(
            ltm=self.ltm, memory_store=self.memory_store, summarizer=_FakeSummarizer()
        )
        self.sid = self.ltm.create_session("alice", title="销售")
        self.ltm.save_conversation_pair("alice", "查销售", "销售100万", session_id=self.sid)

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_concurrent_finalize_and_recall_serialized(self):
        """后台 finalize（写）与请求 recall（读）并发：加锁串行，不抛 database locked、终态一致。"""
        errors = []

        def finalize_loop():
            try:
                for _ in range(15):
                    self.recall.finalize_session(self.sid, "alice")
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=finalize_loop)
        t.start()
        try:
            for _ in range(15):
                self.recall.recall("销售", "alice", exclude_session_id="other")
        except Exception as e:
            errors.append(e)
        t.join()
        self.assertFalse(errors, f"并发访问出错: {errors}")
        # 终态一致：A 摘要仍可召回
        self.assertIn("销售", self.recall.recall("销售", "alice", exclude_session_id="other"))


if __name__ == "__main__":
    unittest.main()
