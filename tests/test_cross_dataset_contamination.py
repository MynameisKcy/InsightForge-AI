"""
Regression for the cross-dataset contamination bug.

Scenario: a user has TWO datasets registered under the same user_id:
  - 山东省人口数据集 (city-level Shandong population)
  - World Bank WDI dataset (with country_name = "Switzerland" / "Germany")

When the user asks "分析山东省人口":
  1. DataResolver correctly picks shandong_pop as primary.
  2. init_duckdb + _reload_datasets_into_instance loads BOTH tables into the
     same :memory: DuckDB connection (because the user owns both).
  3. SQLAgent generates SQL from a prompt that — if unscoped — leaks the
     worldbank_wdi table to the LLM, inviting a cross-table JOIN that mixes
     山东 cities with Switzerland / Germany rows.

Fix: get_enhanced_schema_text(tables=[primary]) scopes the schema; SQLAgent
receives primary_table from PlannerAgent and threads it through to the
schema call + a hard prompt directive banning cross-table SQL.

We assert the post-fix invariant end-to-end by:
  - Stubbing SQLAgent._call_llm to capture the system prompt it sends to the
    LLM, and asserting worldbank_wdi is NOT in the prompt while
    shandong_pop IS.
  - Also asserting the helper get_enhanced_schema_text(tables=[...]) itself
    respects the filter.

No real LLM call: the LLM is non-deterministic. The bug's root cause is in
the data layer's failure to scope the schema, which is what we assert.
"""

import csv
import os
import unittest


def _create_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


class TestCrossDatasetContamination(unittest.TestCase):
    """Regression for the cross-dataset contamination bug."""

    user_id = "test_contam_user"

    def setUp(self):
        from utils.path_tool import get_abs_path
        self.tmp_dir = get_abs_path("data/datasets")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._created_files = []

        # Save and replace the datasources_db module-level singleton so the
        # test reads from an isolated tmp SQLite DB instead of the live one.
        import database.datasources_db as dsd
        from database.datasources_db import DatasourcesDB
        self._dsd_module = dsd
        self._orig_singleton = dsd.datasources_db
        self._tmp_db_path = os.path.join(self.tmp_dir, "test_resolver.db")
        if os.path.exists(self._tmp_db_path):
            os.remove(self._tmp_db_path)
        self._test_db = DatasourcesDB(db_path=self._tmp_db_path)
        dsd.datasources_db = self._test_db

    def tearDown(self):
        for p in self._created_files:
            try:
                os.remove(p)
            except OSError:
                pass
        # Restore the module-level singleton
        try:
            self._dsd_module.datasources_db = self._orig_singleton
        except Exception:
            pass
        # Drop the DuckDB instance so subsequent tests start clean
        try:
            from database.duckdb_manager import close_duckdb
            close_duckdb(self.user_id)
        except Exception:
            pass
        try:
            os.remove(self._tmp_db_path)
        except OSError:
            pass

    def _shandong_csv(self):
        path = os.path.join(self.tmp_dir, "test_shandong_pop.csv")
        if os.path.exists(path):
            os.remove(path)
        _create_csv(path, [
            {"city": "济南市", "population": 9202432, "year": 2020},
            {"city": "青岛市", "population": 10071722, "year": 2020},
            {"city": "烟台市", "population": 7102116, "year": 2020},
        ])
        self._created_files.append(path)
        return path

    def _worldbank_csv(self):
        path = os.path.join(self.tmp_dir, "test_wdi.csv")
        if os.path.exists(path):
            os.remove(path)
        _create_csv(path, [
            {"country_name": "Switzerland", "indicator": "POP", "value": 8703400, "year": 2020},
            {"country_name": "Germany", "indicator": "POP", "value": 83783985, "year": 2020},
        ])
        self._created_files.append(path)
        return path

    # --- Resolver-level guard (sanity check the upstream invariant) ---------

    def test_resolver_picks_shandong_for_shandong_query(self):
        """When the query clearly targets Shandong, the resolver must return
        shandong_pop as primary. (The bug is downstream of this point.)
        """
        from database import data_resolver

        self._test_db.add_dataset(
            "shandong_pop", "csv", self._shandong_csv(), "shandong_pop", "[]", 3,
            display_name="山东省人口普查数据集",
            owner_user_id=self.user_id,
        )
        self._test_db.add_dataset(
            "worldbank_wdi", "csv", self._worldbank_csv(), "worldbank_wdi", "[]", 2,
            display_name="World Bank WDI 跨国数据",
            owner_user_id=self.user_id,
        )

        r = data_resolver.DataResolver.resolve("分析山东省人口", user_id=self.user_id)
        self.assertEqual(
            r["name"], "shandong_pop",
            f"Resolver picked wrong primary dataset: {r['name']} (matched_by={r['matched_by']})"
        )

    # --- Helper-level guard: get_enhanced_schema_text(tables=[...]) ---------

    def test_get_enhanced_schema_text_with_filter_scopes_output(self):
        """DuckDBManager.get_enhanced_schema_text(tables=[...]) must filter
        the rendered schema to the requested table names — the building block
        of the SQL-agent scoped prompt.
        """
        from database.duckdb_manager import init_duckdb

        self._test_db.add_dataset(
            "shandong_pop", "csv", self._shandong_csv(), "shandong_pop", "[]", 3,
            display_name="山东省人口普查数据集",
            owner_user_id=self.user_id,
        )
        self._test_db.add_dataset(
            "worldbank_wdi", "csv", self._worldbank_csv(), "worldbank_wdi", "[]", 2,
            display_name="World Bank WDI 跨国数据",
            owner_user_id=self.user_id,
        )

        # Drive init_duckdb the same way PlannerAgent does — it loads BOTH
        # tables into the same :memory: connection.
        db_inst = init_duckdb(csv_path=self._shandong_csv(), user_id=self.user_id)
        all_tables = set(db_inst.get_table_names())
        self.assertIn("shandong_pop", all_tables)
        self.assertIn("worldbank_wdi", all_tables,
                      "precondition: both datasets should be loaded into the same connection")

        # Baseline: unfiltered call exposes both tables (this is the old behavior,
        # preserved for tests/diagnostics that want a full picture).
        unfiltered = db_inst.get_enhanced_schema_text()
        self.assertIn("shandong_pop", unfiltered)
        self.assertIn("worldbank_wdi", unfiltered)

        # Post-fix: filtered call scopes the schema to the requested table.
        filtered = db_inst.get_enhanced_schema_text(tables=["shandong_pop"])
        self.assertIn("shandong_pop", filtered)
        self.assertNotIn(
            "worldbank_wdi", filtered,
            f"get_enhanced_schema_text(tables=['shandong_pop']) leaked worldbank_wdi. "
            f"This is the contamination source that lets the LLM JOIN the WDI "
            f"table into a 山东 population query.\n--- filtered ---\n{filtered}\n--- end ---"
        )

        # Backward compat: tables=None is the same as the unfiltered call.
        none_filtered = db_inst.get_enhanced_schema_text(tables=None)
        self.assertIn("shandong_pop", none_filtered)
        self.assertIn("worldbank_wdi", none_filtered)

    # --- End-to-end: SQLAgent with primary_table scopes the LLM prompt -----

    def test_sql_agent_prompt_scoped_to_primary_table(self):
        """End-to-end regression for the contamination: when SQLAgent is given
        primary_table='shandong_pop', the system prompt it sends to the LLM
        must NOT mention worldbank_wdi and must include the hard instruction
        to limit SQL to shandong_pop.

        This is the actual user-facing bug surface: if the prompt leaks the
        unrelated table, the LLM may write a cross-table JOIN and the
        resulting dataframe ends up mixing 山东 cities with Switzerland /
        Germany rows — which then flow into the comparison chart.
        """
        from agents.sql_agent import SQLAgent
        from database import data_resolver
        from database.duckdb_manager import init_duckdb

        self._test_db.add_dataset(
            "shandong_pop", "csv", self._shandong_csv(), "shandong_pop", "[]", 3,
            display_name="山东省人口普查数据集",
            owner_user_id=self.user_id,
        )
        self._test_db.add_dataset(
            "worldbank_wdi", "csv", self._worldbank_csv(), "worldbank_wdi", "[]", 2,
            display_name="World Bank WDI 跨国数据",
            owner_user_id=self.user_id,
        )

        # Drive the same path PlannerAgent takes to set up the user instance.
        resolved = data_resolver.DataResolver.resolve("分析山东省人口", user_id=self.user_id)
        self.assertEqual(resolved["name"], "shandong_pop")

        # Force a fresh DuckDB instance for this user with the primary CSV.
        init_duckdb(csv_path=resolved["csv_path"], user_id=self.user_id)
        all_tables = set(init_duckdb(user_id=self.user_id).get_table_names())
        self.assertIn("shandong_pop", all_tables)
        self.assertIn("worldbank_wdi", all_tables,
                      "precondition: both tables should be in the user's DuckDB instance")

        # Build the SQLAgent the way PlannerAgent does (user_id is enough to
        # bind to the right DuckDB instance). We don't call run() because it
        # would execute the SQL — we just want to capture the prompt.
        agent = SQLAgent(user_id=self.user_id)

        captured = {}
        real_call_llm = agent._call_llm

        def stub_call_llm(messages):
            captured["messages"] = messages
            # Return a benign SQL so _extract_sql yields something; we won't
            # execute it. _call_llm is called once in _generate_sql.
            return "```sql\nSELECT 1;\n```"

        agent._call_llm = stub_call_llm
        try:
            agent._generate_sql("分析山东省人口", primary_table="shandong_pop")
        finally:
            agent._call_llm = real_call_llm

        self.assertIn("messages", captured, "_call_llm was not invoked")
        system_prompt = captured["messages"][0]["content"]

        self.assertIn(
            "shandong_pop", system_prompt,
            f"primary table not in prompt — the LLM wouldn't know what to query.\n"
            f"--- prompt ---\n{system_prompt}\n--- end ---"
        )
        self.assertNotIn(
            "worldbank_wdi", system_prompt,
            f"unrelated table worldbank_wdi leaked into the SQL agent prompt. "
            f"This is the contamination path: the LLM sees the WDI table and "
            f"may JOIN it into a 山东 population query, producing Swiss/German "
            f"rows in the resulting chart.\n--- prompt ---\n{system_prompt}\n--- end ---"
        )
        self.assertNotIn(
            "Switzerland", system_prompt,
            f"WDI country values leaked into the prompt — even the column "
            f"VALUE list for worldbank_wdi should not be visible.\n"
            f"--- prompt ---\n{system_prompt}\n--- end ---"
        )
        # The hard instruction must be present.
        self.assertIn("目标数据集已限定为", system_prompt,
                      "hard directive banning cross-table SQL is missing from the prompt")

    def test_sql_agent_prompt_unscoped_when_no_primary_table(self):
        """Backward compat: when SQLAgent is called WITHOUT primary_table
        (e.g. as a standalone tool, not via PlannerAgent), the prompt must
        still expose all tables in the connection (old behavior) and use the
        permissive directive.
        """
        from agents.sql_agent import SQLAgent
        from database.duckdb_manager import init_duckdb

        self._test_db.add_dataset(
            "shandong_pop", "csv", self._shandong_csv(), "shandong_pop", "[]", 3,
            display_name="山东省人口普查数据集",
            owner_user_id=self.user_id,
        )
        self._test_db.add_dataset(
            "worldbank_wdi", "csv", self._worldbank_csv(), "worldbank_wdi", "[]", 2,
            display_name="World Bank WDI 跨国数据",
            owner_user_id=self.user_id,
        )

        init_duckdb(csv_path=self._shandong_csv(), user_id=self.user_id)

        agent = SQLAgent(user_id=self.user_id)
        captured = {}

        def stub_call_llm(messages):
            captured["messages"] = messages
            return "```sql\nSELECT 1;\n```"

        real = agent._call_llm
        agent._call_llm = stub_call_llm
        try:
            agent._generate_sql("跨数据集关联分析", primary_table="")
        finally:
            agent._call_llm = real

        system_prompt = captured["messages"][0]["content"]
        # Backward compat: both tables exposed, old cross-table JOIN directive present.
        self.assertIn("shandong_pop", system_prompt)
        self.assertIn("worldbank_wdi", system_prompt)
        self.assertIn("跨表查询时请使用标准 SQL JOIN", system_prompt)
        # The scoped directive should NOT be present (primary_table was empty).
        self.assertNotIn("目标数据集已限定为", system_prompt)

    # --- P0-A regression: static fallback must not feed display name as table ----

    def test_planner_static_fallback_scoped_table_is_transactions(self):
        """静态内置数据集（DATASET_MAP fallback）下 planner 下传的 primary_table
        必须是真实 DuckDB 表名 transactions，而不是带空格的 display name
        'Superstore Sales Dataset' —— 后者撞 validate_table_name SecurityError，
        全分析 SQL 阶段 100% 失败（2026-08-28 报告 §2 P0-A；此前仅动态路径
        有测试，静态 fallback 是零覆盖盲区）。"""
        from agents.planner_agent import PlannerAgent
        from database.data_resolver import DataResolver

        # 不 add 任何动态数据集 → resolver 走 DATASET_MAP 静态 fallback
        resolved = DataResolver.resolve(
            "分析 Superstore 超市销售额趋势", user_id=self.user_id)
        self.assertEqual(resolved["name"], "Superstore Sales Dataset")
        self.assertEqual(resolved["primary_table"], "transactions")

        # planner 装配层（__new__ 规避重 __init__ 建 7 个子 agent + LLM）
        planner = PlannerAgent.__new__(PlannerAgent)
        ctx = planner._resolve_context({
            "user_id": self.user_id,
            "session_id": "s1",
            "query": "分析 Superstore 超市销售额趋势",
        })
        self.assertEqual(ctx.primary_table, "transactions")
        self.assertNotIn(" ", ctx.primary_table)

        # 装配链终点：scoped schema 用该表名不再抛 SecurityError（8-28 崩溃点）。
        # 不用 resolved["csv_path"]（data/train.csv 被 gitignore，CI 干净检出无此文件，
        # 2026-09-03 远端红）——此处验证的是「表名 transactions 过 validate_table_name
        # 且进 schema 文本」，与 CSV 内容无关，迷你临时 CSV 即可等价走完装配链。
        from database.duckdb_manager import init_duckdb

        mini_csv = _create_csv(
            os.path.join(self.tmp_dir, "test_static_fallback_mini.csv"),
            [{"category": "A", "sales": 1}, {"category": "B", "sales": 2}],
        )
        self._created_files.append(mini_csv)
        db = init_duckdb(csv_path=mini_csv, user_id=self.user_id)
        text = db.get_enhanced_schema_text(
            tables=[ctx.primary_table], compact=True)
        self.assertIn("transactions", text)


class TestQuickDataInsightDisambiguation(unittest.TestCase):
    """quick_data_insight 数据集消歧（C3）：命中传 scoped primary_table；
    歧义（多数据集 + query 无特征词）时文本列候选追问、不执行 SQL。"""

    user_id = "test_quick_contam_user"

    def setUp(self):
        from utils.path_tool import get_abs_path
        self.tmp_dir = get_abs_path("data/datasets")
        os.makedirs(self.tmp_dir, exist_ok=True)
        self._created_files = []

        import database.datasources_db as dsd
        from database.datasources_db import DatasourcesDB
        self._dsd_module = dsd
        self._orig_singleton = dsd.datasources_db
        self._tmp_db_path = os.path.join(self.tmp_dir, f"test_quick_{self.user_id}.db")
        if os.path.exists(self._tmp_db_path):
            os.remove(self._tmp_db_path)
        self._test_db = DatasourcesDB(db_path=self._tmp_db_path)
        dsd.datasources_db = self._test_db

    def tearDown(self):
        for p in self._created_files:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            self._dsd_module.datasources_db = self._orig_singleton
        except Exception:
            pass
        try:
            from database.duckdb_manager import close_duckdb
            close_duckdb(self.user_id)
        except Exception:
            pass
        try:
            os.remove(self._tmp_db_path)
        except OSError:
            pass

    def _csv(self, name, rows):
        path = os.path.join(self.tmp_dir, name)
        if os.path.exists(path):
            os.remove(path)
        _create_csv(path, rows)
        self._created_files.append(path)
        return path

    def _add_two_datasets(self):
        from database.duckdb_manager import init_duckdb
        shandong = self._csv("test_quick_shandong.csv", [
            {"city": "济南市", "population": 9202432},
            {"city": "青岛市", "population": 10071722},
        ])
        wdi = self._csv("test_quick_wdi.csv", [
            {"country_name": "Switzerland", "value": 8703400},
            {"country_name": "Germany", "value": 83783985},
        ])
        # 真实装载：init_duckdb 会 load 两个表进同一 user 实例（与污染场景同构）
        init_duckdb(csv_path=shandong, user_id=self.user_id)
        init_duckdb(csv_path=wdi, user_id=self.user_id)
        self._test_db.add_dataset(
            "test_quick_shandong", "csv", shandong, "test_quick_shandong", "[]", 2,
            display_name="山东省人口普查数据集",
            owner_user_id=self.user_id,
        )
        self._test_db.add_dataset(
            "test_quick_wdi", "csv", wdi, "test_quick_wdi", "[]", 2,
            display_name="World Bank WDI 跨国数据",
            owner_user_id=self.user_id,
        )
        return shandong, wdi

    def test_ambiguous_query_asks_user_without_running_sql(self):
        """query 无任何数据集特征词 + 用户有多数据集 → 列候选追问，SQLAgent 不被调用。"""
        from unittest.mock import patch

        from agent.tools.agent_tools import quick_data_insight
        self._add_two_datasets()

        called = []

        class _FakeSQLAgent:
            def __init__(self, user_id=None, **k):
                called.append("sql_constructed")

            def run(self, payload):
                called.append("sql_run")
                return {"dataframe_json": "[]", "row_count": 0}

        token = _set_request_ctx(self.user_id)
        try:
            with patch("agents.sql_agent.SQLAgent", _FakeSQLAgent):
                out = quick_data_insight.invoke({"query": "整体趋势怎么样"})
        finally:
            _reset_request_ctx(token)

        self.assertNotIn("sql_run", called, "歧义时不应执行 SQL")
        self.assertIn("请告诉我查询哪一个", out)
        self.assertIn("山东省人口普查数据集", out)
        self.assertIn("World Bank WDI 跨国数据", out)

    def test_clear_query_passes_primary_table_to_sql(self):
        """query 明确指向某数据集 → SQLAgent 收到 scoped primary_table。"""
        from unittest.mock import patch

        from agent.tools.agent_tools import quick_data_insight
        self._add_two_datasets()

        seen = {}

        class _FakeSQLAgent:
            def __init__(self, user_id=None, **k):
                seen["user_id"] = user_id

            def run(self, payload):
                seen["primary_table"] = payload.get("primary_table", "")
                return {"dataframe_json": '[{"city":"济南市","population":9202432}]',
                        "row_count": 1}

        token = _set_request_ctx(self.user_id)
        try:
            with patch("agents.sql_agent.SQLAgent", _FakeSQLAgent):
                out = quick_data_insight.invoke({"query": "山东省人口有多少"})
        finally:
            _reset_request_ctx(token)

        self.assertEqual(seen["primary_table"], "test_quick_shandong")
        self.assertIn("查询结果", out)

    def test_ambiguous_when_multiple_unmatched_static_fallback_still_runs(self):
        """无动态数据集（静态内置）时保持原行为：不歧义、照常执行（向后兼容）。"""
        from unittest.mock import patch

        from agent.tools.agent_tools import quick_data_insight
        # 不 add 任何数据集 → resolver 走 DATASET_MAP 静态 fallback

        called = []

        class _FakeSQLAgent:
            def __init__(self, user_id=None, **k):
                pass

            def run(self, payload):
                called.append(payload.get("primary_table", ""))
                return {"dataframe_json": '[{"a": 1}]', "row_count": 1}

        token = _set_request_ctx(self.user_id)
        try:
            with patch("agents.sql_agent.SQLAgent", _FakeSQLAgent):
                out = quick_data_insight.invoke({"query": "销售额是多少"})
        finally:
            _reset_request_ctx(token)

        self.assertEqual(len(called), 1, "静态数据集路径应照常执行 SQL")
        self.assertIn("查询结果", out)


def _set_request_ctx(user_id):
    from utils.request_context import set_request_context
    return set_request_context(user_id=user_id)


def _reset_request_ctx(token):
    from utils.request_context import reset_request_context
    reset_request_context(token)


if __name__ == "__main__":
    unittest.main()
