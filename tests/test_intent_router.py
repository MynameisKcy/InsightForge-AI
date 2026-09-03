"""agent/tools/intent_router.py 规则分类器单元测试。

分类器是纯函数，15+ 用例覆盖三档意图 + 规则优先级 + 边界 case。
不依赖 LLM、数据库、FastAPI——秒级跑完。

测试矩阵：
- CHAT：问候/致谢/确认
- ANALYSIS：分析/出图/报告/对比/趋势 + 长 query
- QUERY：X 是多少/谁最多/统计/top + 短问句
- 边界：空 query / 默认走全任务 / 大小写不敏感
"""

import pytest

from agent.tools.intent_router import Intent, classify_intent, is_intent


# ── CHAT 类 ─────────────────────────────────────
class TestChatIntent:
    def test_greeting_hello(self):
        r = classify_intent("你好")
        assert r.intent == Intent.CHAT
        assert r.confidence == "high"

    def test_greeting_hey(self):
        r = classify_intent("hey 在吗")
        assert r.intent == Intent.CHAT

    def test_thanks(self):
        r = classify_intent("谢谢")
        assert r.intent == Intent.CHAT

    def test_thanks_english(self):
        r = classify_intent("Thanks a lot")
        assert r.intent == Intent.CHAT

    def test_ack(self):
        r = classify_intent("好的")
        assert r.intent == Intent.CHAT

    def test_ack_ok(self):
        r = classify_intent("OK 我知道了")
        assert r.intent == Intent.CHAT

    def test_ack_prefix_with_analysis_stays_analysis(self):
        # 回归：确认语前缀 + 显式分析意图 → ANALYSIS（误路由修正：
        # 问候前缀不得裁掉 run_full_analysis）
        for q in ("好的，帮我分析一下销售趋势",
                  "明白，对比各区人口",
                  "OK，分析下利润变化",
                  "收到，帮我生成报告"):
            r = classify_intent(q)
            assert r.intent == Intent.ANALYSIS, f"{q!r} -> {r.intent}"
            assert r.matched_rule.startswith("keyword:"), f"{q!r} -> {r.matched_rule}"

    def test_ack_prefix_pure_confirmation_stays_chat(self):
        # 无分析关键词的确认语仍走 CHAT（"好的"规则保留）
        for q in ("好的，明白了", "OK 没问题", "收到，谢谢"):
            r = classify_intent(q)
            assert r.intent == Intent.CHAT, f"{q!r} -> {r.intent}"


# ── ANALYSIS 类（核心）────────────────────
class TestAnalysisIntent:
    def test_explicit_analyze(self):
        r = classify_intent("分析3月销售趋势")
        assert r.intent == Intent.ANALYSIS
        assert "分析" in r.matched_rule

    def test_compare(self):
        r = classify_intent("对比各区人口分布")
        assert r.intent == Intent.ANALYSIS

    def test_trend(self):
        r = classify_intent("最近一年的流量趋势")
        assert r.intent == Intent.ANALYSIS

    def test_draw_chart(self):
        r = classify_intent("画一幅趋势图")
        assert r.intent == Intent.ANALYSIS

    def test_visualize(self):
        r = classify_intent("可视化路口流量变化")
        assert r.intent == Intent.ANALYSIS

    def test_generate_report(self):
        r = classify_intent("生成3月销售分析报告")
        assert r.intent == Intent.ANALYSIS

    def test_chart_type_zh(self):
        r = classify_intent("做个饼图")
        assert r.intent == Intent.ANALYSIS

    def test_chart_type_en(self):
        r = classify_intent("plot a bar chart")
        assert r.intent == Intent.ANALYSIS

    def test_long_query_default_analysis(self):
        # 长 query 命中"对比/分析"关键词 → ANALYSIS，matched_rule 是 keyword
        # （如果未来去掉了所有 ANALYSIS 关键词才会落到 long_query 规则）
        r = classify_intent(
            "请帮我把最近 12 个月每个区域的人口增长率做一个详细的对比分析，"
            "包括同比环比、异常点识别和趋势预测，并生成可视化图表和完整报告"
        )
        assert r.intent == Intent.ANALYSIS

    def test_long_query_no_keywords_falls_to_long_query(self):
        # 长 query (> 80 字) 但无任何分析/可视化/对比/趋势等关键词 → 命中 long_query 规则
        # （fixture 必须真超过 80 字——旧夹具 73 字只到 default 规则，见实测修正）
        r = classify_intent(
            "这个月所有用户对系统的访问记录情况包括每一天的具体时间点和使用时长"
            "还有每个用户的行为细节都要整理成一份完整的文字材料给我看"
            "并且把登录次数页面停留时长和操作频率这些字段全部列出来供我逐项核对"
        )
        assert r.intent == Intent.ANALYSIS
        assert r.matched_rule == "long_query"
        assert r.confidence == "low"

    def test_case_insensitive(self):
        r1 = classify_intent("ANALYZE sales trend")
        r2 = classify_intent("analyze sales trend")
        assert r1.intent == r2.intent == Intent.ANALYSIS


# ── QUERY 类（单点查询）────────────────
class TestQueryIntent:
    def test_how_much(self):
        r = classify_intent("3月销售多少？")
        assert r.intent == Intent.QUERY

    def test_which_most(self):
        r = classify_intent("哪个地区人口最多？")
        assert r.intent == Intent.QUERY

    def test_top_n(self):
        r = classify_intent("TOP 5 客户是哪些？")
        assert r.intent == Intent.QUERY

    def test_count(self):
        r = classify_intent("查一下总用户数？")
        assert r.intent == Intent.QUERY

    def test_short_question(self):
        # 短问句（< 20 字 + 问号）→ QUERY
        r = classify_intent("有数据吗？")
        assert r.intent == Intent.QUERY

    def test_verb_query_no_question(self):
        # 显式查询动词（无问号也行）→ QUERY
        r = classify_intent("查3月销售")
        assert r.intent == Intent.QUERY

    def test_avg_english(self):
        r = classify_intent("what is the average order value?")
        assert r.intent == Intent.QUERY


# ── 默认 / 边界 ─────────────────
class TestDefaults:
    def test_empty_query_defaults_to_analysis(self):
        # 空 query 项目策略：默认走全任务
        r = classify_intent("")
        assert r.intent == Intent.ANALYSIS
        assert r.matched_rule == "empty"

    def test_whitespace_only(self):
        r = classify_intent("   \t  ")
        assert r.intent == Intent.ANALYSIS

    def test_ambiguous_short_no_question_defaults_to_analysis(self):
        # 短、无问号、无关键词 → 默认走全任务（保守）
        r = classify_intent("看一下")
        # "看" 是分析弱信号吗？检查实际行为：
        # 当前实现里 "看" 不在 _ANALYSIS_KEYWORDS；"看一下" 也不在 _QUERY_KEYWORDS 的动词列表
        # len < 20 但无问号 → 走默认 ANALYSIS
        assert r.intent == Intent.ANALYSIS

    def test_unrecognized_defaults_to_analysis(self):
        # 完全无法判定的"中等长度"query → 默认 ANALYSIS
        r = classify_intent("给我讲个故事")
        assert r.intent == Intent.ANALYSIS

    def test_priority_analysis_beats_query_keyword(self):
        # "分析多少" 包含 "分析"（ANALYSIS）和 "多少"（QUERY）→ ANALYSIS 优先
        r = classify_intent("分析一下3月销售多少")
        assert r.intent == Intent.ANALYSIS

    def test_priority_analysis_keyword_beats_greeting(self):
        # 回归：问候前缀 + 显式分析词 → ANALYSIS（旧语义"问候压过一切"
        # 会把"你好，帮我分析一下销售趋势"误路由成 CHAT，裁掉 run_full_analysis）
        r = classify_intent("你好，帮我分析一下销售趋势")
        assert r.intent == Intent.ANALYSIS
        assert r.matched_rule.startswith("keyword:")


# ── LLM few-shot 分类器（主路径）────────────────
class TestLlmClassifier:
    """classify_intent_llm / classify_with_fallback：mock 模型，验证映射与回退。"""

    @staticmethod
    def _patch_llm(out):
        from unittest.mock import patch
        return patch("agents.base.BaseAgent._call_llm_with_schema", return_value=out)

    def test_llm_maps_three_intents(self):
        from agent.tools.intent_router import classify_intent_llm
        for raw, expect in (("chat", Intent.CHAT),
                            ("query", Intent.QUERY),
                            ("analysis", Intent.ANALYSIS)):
            with self._patch_llm({"intent": raw}):
                r = classify_intent_llm("随便什么")
            assert r.intent == expect
            assert r.matched_rule == "llm_fewshot"

    def test_schema_failure_raises(self):
        from agent.tools.intent_router import classify_intent_llm
        with self._patch_llm({"intent": "fly"}), pytest.raises(ValueError):
            classify_intent_llm("x")

    def test_fallback_uses_rules_when_llm_fails(self):
        from unittest.mock import patch

        from agent.tools import intent_router
        # LLM 抛异常 → 回退规则："分析3月销售趋势" 规则命中 ANALYSIS
        with patch.object(intent_router, "classify_intent_llm",
                          side_effect=RuntimeError("llm down")):
            r = intent_router.classify_with_fallback("分析3月销售趋势")
        assert r.intent == Intent.ANALYSIS
        assert r.matched_rule.startswith("keyword:")

    def test_fallback_handles_confirm_prefix_via_llm(self):
        # few-shot 已含"好的，帮我分析一下销售趋势"示例——模型被教会该边界；
        # 这里验证 LLM 返回 analysis 时正确透传（规则层处理不了靠 LLM 补）
        from agent.tools.intent_router import classify_with_fallback
        with self._patch_llm({"intent": "analysis"}):
            r = classify_with_fallback("好的，帮我分析一下销售趋势")
        assert r.intent == Intent.ANALYSIS
        assert r.matched_rule == "llm_fewshot"

    def test_prompt_contains_fewshot_examples(self):
        from agent.tools.intent_router import _build_classify_messages
        msgs = _build_classify_messages("分析销售")
        body = msgs[0]["content"]
        assert "好的，帮我分析一下销售趋势" in body   # 边界样本
        assert "3月销售多少？" in body              # query 样本
        assert "意图：" in body and "分析销售" in body  # 当前 query 拼接

    def test_binds_thinking_off_regardless_of_global(self, monkeypatch):
        """意图分类无条件 bind 关思考：全局开关即使为 true 也不影响此热路径调用。

        差分实测（2026-09-03）：分类被思考 token 拖到 2.5~7s，关后 0.43s。
        """
        from agent.tools.intent_router import classify_intent_llm
        from agents.base import BaseAgent

        captured = {}

        class _FakeModel:
            def bind(self, **kwargs):
                captured["bind_kwargs"] = kwargs
                return self

        fake = _FakeModel()
        monkeypatch.setattr("model.factory.get_chat_model", lambda uid=None: fake)

        def fake_call(self, messages, schema, retries=1):
            captured["agent_model"] = self.model
            return {"intent": "chat"}

        monkeypatch.setattr(BaseAgent, "_call_llm_with_schema", fake_call)
        r = classify_intent_llm("你好")
        assert r.intent == Intent.CHAT
        assert captured["bind_kwargs"] == {"extra_body": {"enable_thinking": False}}
        # 传给 BaseAgent 的就是 bind 后的模型实例
        assert captured["agent_model"] is fake


# ── 归一化（剥离会话引导词）────────────────
class TestNormalization:
    def test_normalize_strips_confirm_prefix(self):
        from agent.tools.intent_router import _normalize_query
        assert _normalize_query("好的，帮我分析一下销售趋势") == "分析一下销售趋势"
        assert _normalize_query("明白，对比各区人口") == "对比各区人口"
        assert _normalize_query("OK，分析下利润变化") == "分析下利润变化"

    def test_normalize_pure_greeting_keeps_original(self):
        from agent.tools.intent_router import _normalize_query
        assert _normalize_query("你好") == "你好"
        assert _normalize_query("好的，明白了") == "好的，明白了"

    def test_normalize_english_word_boundary(self):
        from agent.tools.intent_router import _normalize_query
        # ok/okay 独立出现可剥
        assert _normalize_query("ok 帮我分析") == "分析"
        assert _normalize_query("okay，分析趋势") == "分析趋势"
        # ok 是更长词的前缀时不剥（词边界保护，防 oklunchtime 被切成 lunchtime）
        assert _normalize_query("oklunchtime") == "oklunchtime"

    def test_rules_handle_confirm_prefix_after_normalization(self):
        # 归一化后规则层也能直接处理"确认语+分析"（无需 LLM 兜底）
        for q in ("好的，帮我分析一下销售趋势",
                  "你好，帮我对比各区人口"):
            r = classify_intent(q)
            assert r.intent == Intent.ANALYSIS, f"{q!r} -> {r.intent}"
            assert r.matched_rule.startswith("keyword:")

    def test_llm_path_receives_normalized_body(self):
        # classify_with_fallback 归一化一次，LLM 与规则吃同一主体
        from unittest.mock import patch

        from agent.tools import intent_router
        seen = {}

        def fake_llm(query, user_id=None):
            seen["query"] = query
            raise RuntimeError("force fallback")

        with patch.object(intent_router, "classify_intent_llm", side_effect=fake_llm):
            r = intent_router.classify_with_fallback("好的，帮我分析一下销售趋势")
        assert seen["query"] == "分析一下销售趋势"   # LLM 收到归一化主体
        assert r.intent == Intent.ANALYSIS           # 规则兜底同样命中


# ── is_intent 便捷包装 ─────────────────
class TestIsIntentHelper:
    def test_is_intent_chat(self):
        assert is_intent("你好", Intent.CHAT) is True
        assert is_intent("分析销售", Intent.CHAT) is False

    def test_is_intent_query(self):
        assert is_intent("3月销售多少", Intent.QUERY) is True
        assert is_intent("分析销售", Intent.QUERY) is False

    def test_is_intent_analysis(self):
        assert is_intent("分析销售", Intent.ANALYSIS) is True
        assert is_intent("3月销售多少", Intent.ANALYSIS) is False


# ── 返回结构 ─────────────────
class TestResultStructure:
    def test_result_has_required_fields(self):
        r = classify_intent("分析销售")
        assert hasattr(r, "intent")
        assert hasattr(r, "matched_rule")
        assert hasattr(r, "confidence")
        assert r.confidence in ("high", "medium", "low", "default")

    def test_intent_is_enum(self):
        from enum import Enum
        r = classify_intent("你好")
        assert isinstance(r.intent, Intent)
        assert issubclass(type(r.intent), Enum)

    def test_intent_str_value(self):
        # Intent 是 str Enum，可直接 str() 拿到值
        r = classify_intent("你好")
        assert r.intent.value == "chat"
        assert str(r.intent) == "Intent.CHAT"


# ── 工具档位目录契约（ADR-0004）────────────────
class TestToolCatalogContract:
    """agent_tools.for_intent 契约：阶梯嵌套 / analysis=全集 / 关键成员 / 身份稳定。

    目录表单一真相源（_TOOL_MIN_INTENT），本类断言其推导不变量；
    不测快照数量（数量是推导结果，加工具不应拖累测试）。
    """

    @staticmethod
    def _names(intent):
        from agent.tools.agent_tools import for_intent
        return {t.name for t in for_intent(intent)}

    def test_ladder_strict_nesting(self):
        # 单调阶梯 chat ⊂ query ⊂ analysis（严格嵌套，逐档递增）
        chat = self._names(Intent.CHAT)
        query = self._names(Intent.QUERY)
        analysis = self._names(Intent.ANALYSIS)
        assert chat < query, "CHAT 应为 QUERY 的真子集"
        assert query < analysis, "QUERY 应为 ANALYSIS 的真子集"

    def test_analysis_is_full_catalog(self):
        # analysis 档 = 全集：与目录表条目一一对应（Q5 恒等式）
        from agent.tools.agent_tools import _TOOL_MIN_INTENT, for_intent
        analysis_names = {t.name for t in for_intent(Intent.ANALYSIS)}
        catalog_names = {t.name for t, _ in _TOOL_MIN_INTENT}
        assert analysis_names == catalog_names
        assert len(analysis_names) == len(_TOOL_MIN_INTENT)  # 无重复工具

    def test_key_membership(self):
        # 关键归属：run_full_analysis 仅 analysis；rag_sumarize 三档可见；
        # get_external_data 仅 analysis（不进 QUERY 快查）
        chat = self._names(Intent.CHAT)
        query = self._names(Intent.QUERY)
        analysis = self._names(Intent.ANALYSIS)
        assert "run_full_analysis" not in chat
        assert "run_full_analysis" not in query
        assert "run_full_analysis" in analysis
        assert "rag_sumarize" in chat and "rag_sumarize" in query and "rag_sumarize" in analysis
        assert "get_external_data" not in query
        assert "get_external_data" in analysis

    def test_tool_identity_stable(self):
        # 多次调用返回同一批工具对象（同序、无重复），防列表重建回归
        from agent.tools.agent_tools import for_intent
        a = for_intent(Intent.ANALYSIS)
        b = for_intent(Intent.ANALYSIS)
        assert len(a) == len(b) > 0
        assert all(x is y for x, y in zip(a, b)), "两次调用应返回同一批对象"
        assert len({id(t) for t in a}) == len(a), "同一工具不得重复出现"

    def test_mode_effect_only_for_report_trigger(self):
        # 模式副作用（ADR-0004 扩展）：仅 fill_report_context_for_report 声明 report 效应，
        # 普通工具返回 None（monitor_tool 依此通用置位，替代名字魔法串）
        from agent.tools.agent_tools import (
            _TOOL_MODE_EFFECT,
            REPORT_MODE,
            fill_report_context_for_report,
            mode_effect_for,
        )
        assert _TOOL_MODE_EFFECT == [(fill_report_context_for_report, REPORT_MODE)]
        assert mode_effect_for("fill_report_context_for_report") == REPORT_MODE
        assert mode_effect_for("run_full_analysis") is None
        assert mode_effect_for("rag_sumarize") is None
        assert mode_effect_for("no.such.tool") is None


# ── dynamic_toolset 接线契约（C4：middleware 裸接线补测试）────────
class TestDynamicToolsetWiring:
    """dynamic_toolset：首条消息分类一次 + 按 for_intent 裁剪 + 异常落 ANALYSIS。

    用桩 request（.runtime.context dict / .messages / .override）直调
    middleware 函数，不经过 LangChain 运行时；patch 分类入口隔离 LLM。
    """

    @staticmethod
    def _make_request(messages):
        from types import SimpleNamespace
        req = SimpleNamespace()
        req.messages = messages
        req.runtime = SimpleNamespace(context={})
        overrides = []
        req.overrides = overrides
        req.override = lambda **kw: overrides.append(kw.get("tools")) or req
        return req

    def _invoke(self, req, handler=None):
        from agent.tools.middleware import dynamic_toolset
        # wrap_model_call 装饰器产物：经 .wrap_model_call 进入（不可直接调用）
        return dynamic_toolset.wrap_model_call(
            req, handler or (lambda r: "handled"))

    def test_classifies_once_caches_by_context(self):
        # 同 request 两次调用：首次分类，第二次命中 runtime.context 缓存，
        # 不重复调分类器（ReAct 循环内多次 model call 只分类一次的契约）
        from unittest.mock import patch

        from agent.tools.intent_router import Intent, IntentResult
        calls = []

        def fake_classify(query, user_id=None):
            calls.append(query)
            return IntentResult(Intent.QUERY, "llm_fewshot", "high")

        req = self._make_request([{"role": "user", "content": "3月销售多少？"}])
        with patch("agent.tools.middleware.classify_with_fallback",
                   side_effect=fake_classify):
            self._invoke(req)
            self._invoke(req)
        assert len(calls) == 1, f"分类应只发生一次，实际 {len(calls)}"

    def test_override_scoped_to_intent_toolset(self):
        # QUERY 意图 → override 收到的工具 == for_intent(QUERY)（不含 run_full_analysis）
        from unittest.mock import patch

        from agent.tools.agent_tools import for_intent
        from agent.tools.intent_router import Intent, IntentResult

        req = self._make_request([{"role": "user", "content": "3月销售多少？"}])
        with patch("agent.tools.middleware.classify_with_fallback",
                   return_value=IntentResult(Intent.QUERY, "llm_fewshot", "high")):
            self._invoke(req)
        assert len(req.overrides) == 1
        got = {t.name for t in req.overrides[0]}
        expect = {t.name for t in for_intent(Intent.QUERY)}
        assert got == expect
        assert "run_full_analysis" not in got, "QUERY 档不得暴露 run_full_analysis"

    def test_classify_failure_falls_back_to_full_toolset(self):
        # 分类器抛异常 → 落 ANALYSIS 档（全集），middleware 不挂
        from unittest.mock import patch

        from agent.tools.agent_tools import for_intent
        from agent.tools.intent_router import Intent

        req = self._make_request([{"role": "user", "content": "分析销售趋势"}])
        with patch("agent.tools.middleware.classify_with_fallback",
                   side_effect=RuntimeError("llm down")):
            self._invoke(req)
        assert len(req.overrides) == 1
        got = {t.name for t in req.overrides[0]}
        expect = {t.name for t in for_intent(Intent.ANALYSIS)}
        assert got == expect
        assert "run_full_analysis" in got, "异常兜底应暴露全集（项目策略）"
