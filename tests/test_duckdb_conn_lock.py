"""DuckDBManager 连接级串行化回归测试（issue #9）。

LangGraph 工具节点并行执行同轮多个工具时，查询通道（query_df /
execute_fetchall）与管理通道（reload_csv→pandas 回退）会在同一条 duckdb
连接上并发——2026-09-03 live 观测到 pandas 回退的 unregister 与并行 COUNT
在 duckdb 原生锁上互锁，查询超时 watchdog 的 interrupt 对"未开始执行"的
锁等待无效，SSE 永久挂起。修复后连接级 RLock 串行化全部 conn 触点。

本测试用真实 :memory: 连接（离线可跑）并发轰查询与重灌，带看门狗：
一旦死锁即 join 超时失败。两个 CSV 交替重灌，强制每次都走真实
DROP+CREATE（reload_csv 对同一文件会早退，不触连接）。
"""
import threading

import pytest

from database.duckdb_manager import DuckDBManager


def _gbk_csv(tmp_path, name, rows):
    """GBK 编码小 CSV：强制 read_csv_auto 失败走 pandas 回退（死锁现场路径）。"""
    p = tmp_path / name
    lines = "地区,人口数\n" + "".join(f"城市{i},{1000 + i}\n" for i in range(rows))
    p.write_bytes(lines.encode("gbk"))
    return str(p)


@pytest.fixture
def mgr(tmp_path):
    csv_a = _gbk_csv(tmp_path, "a.csv", 3)
    csv_b = _gbk_csv(tmp_path, "b.csv", 5)
    m = DuckDBManager(csv_path=csv_a, user_id="u_conn_lock")
    m._lock_test_csvs = (csv_a, csv_b)
    yield m
    m.close()


def test_parallel_query_and_reload_serialize(mgr):
    """查询与管理通道并行轰 200/20 轮：串行化后 30s 看门狗内必须全部完成。"""
    csv_a, csv_b = mgr._lock_test_csvs
    reloaded = threading.Event()

    def reloader():
        for i in range(20):
            mgr.reload_csv(csv_a if i % 2 == 0 else csv_b)
        reloaded.set()

    t = threading.Thread(target=reloader, daemon=True)
    t.start()
    try:
        for _ in range(200):
            df = mgr.query_df('SELECT COUNT(*) AS cnt FROM "transactions"')
            assert df.iloc[0, 0] in (3, 5)   # 交替重灌的两种行数,无中间态
            if not t.is_alive():
                break
    finally:
        t.join(timeout=30)
    assert reloaded.is_set(), "reload 线程 30s 未完成——连接并发仍会死锁（回归 issue #9）"


def test_execute_fetchall_atomic(mgr):
    """execute_fetchall 结果物化在连接锁内：外部链式调用的锁外窗口已消除。"""
    rows = mgr.execute_fetchall('SELECT COUNT(*) AS cnt FROM "transactions"')
    assert rows[0][0] == 3


def test_interrupt_watchdog_still_fires(mgr, monkeypatch):
    """串行化不得破坏超时 watchdog：卡查询仍被 interrupt 转 TimeoutError。"""
    monkeypatch.setattr(mgr, "_query_timeout", 1)
    # 25 亿行聚合,实测远超 1s(探测跑 2 分钟未完);1s watchdog 应中断转 TimeoutError
    with pytest.raises(TimeoutError):
        mgr.query_df("SELECT sum(a.range + b.range) FROM range(50000) a, range(50000) b")
