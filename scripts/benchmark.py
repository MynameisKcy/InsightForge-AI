#!/usr/bin/env python
"""InsightForge AI — 单用户端到端性能基准（真实走 HTTP + SSE）。

流程：注册/登录 bench 用户 → 检查数据集（无则上传内置样例 CSV）→
5 类查询 × N 次迭代，消费 SSE 流到 [DONE] → 输出 P50/P95/P99/均值 + Token/成本。

用法（服务已启动的前提下）：
    python scripts/benchmark.py                     # 默认 http://localhost:8502，5 轮
    python scripts/benchmark.py --base-url http://localhost:8502 --iterations 3
"""
import argparse
import csv
import io
import json
import random
import statistics
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import requests

QUERIES = [
    "数据里一共有多少条记录？",
    "按月份统计销售额趋势",
    "对比各个区域的销售金额",
    "检测销售额中的异常值",
    "总结这份数据的关键发现",
]


def ensure_auth(base_url: str, password: str) -> str:
    """注册（或登录）bench 用户并返回 Bearer token。"""
    account = f"bench_{uuid.uuid4().hex[:8]}"
    resp = requests.post(f"{base_url}/api/register",
                         json={"account": account, "password": password}, timeout=30)
    body = resp.json()
    if body.get("success") and body.get("token"):
        return body["token"]
    raise SystemExit(f"注册失败: {resp.status_code} {body}")


def ensure_dataset(base_url: str, token: str) -> str:
    """确保 bench 用户至少有一个数据集；没有则上传生成的样例销售 CSV，返回数据集名。"""
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{base_url}/api/datasets", headers=headers, timeout=30)
    datasets = resp.json() if resp.ok else []
    # 兼容返回结构：list 或 {"datasets": [...]}
    if isinstance(datasets, dict):
        datasets = datasets.get("datasets", [])
    if datasets:
        name = datasets[0].get("display_name") or datasets[0].get("name") or datasets[0].get("table_name")
        print(f"  使用已有数据集: {name}")
        return name

    # 生成 200 行样例销售数据
    regions = ["华东", "华北", "华南", "西南", "东北"]
    products = ["云帆CRM标准版", "云帆CRM专业版", "数据洞察模块", "定制开发服务"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["订单日期", "区域", "产品", "销售额", "数量"])
    today = date.today()  # noqa: DTZ011  （基准脚本用本地日期生成样例数据）
    for i in range(200):
        d = today - timedelta(days=random.randint(0, 180))
        writer.writerow([d.isoformat(), random.choice(regions), random.choice(products),
                         round(random.uniform(2000, 50000), 2), random.randint(1, 20)])
    files = {"file": ("bench_sales.csv", buf.getvalue().encode("utf-8"), "text/csv")}
    resp = requests.post(f"{base_url}/api/datasets/upload", headers=headers, files=files, timeout=120)
    body = resp.json() if resp.ok else {}
    if not resp.ok or body.get("error"):
        raise SystemExit(f"数据集上传失败: {resp.status_code} {body}")
    name = body.get("display_name") or body.get("name") or "bench_sales"
    print(f"  已上传样例数据集: {name}")
    return name


def run_one(base_url: str, token: str, query: str) -> tuple[float, dict | None, str]:
    """跑一次对话（每次新会话），返回 (总延迟秒, 该轮最终 METRICS, 错误信息)。

    session_id 传空串让服务端新建会话：传自造 ID 会命中 IDOR 防护直接 404
    （会话不存在即拒绝，且 404 响应体非 SSE，须按 HTTP 状态码判失败）。
    """
    headers = {"Authorization": f"Bearer {token}"}
    start = time.perf_counter()
    last_metrics, error = None, ""
    resp = requests.post(f"{base_url}/api/chat", headers=headers, stream=True, timeout=600,
                         json={"query": query, "session_id": ""})
    if resp.status_code != 200:
        return time.perf_counter() - start, None, f"HTTP {resp.status_code}: {resp.text[:80]}"
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace")
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data.startswith("[METRICS:"):
            try:
                last_metrics = json.loads(data[9:-1].strip())
            except json.JSONDecodeError:
                pass
        elif data.startswith("[ERROR]"):
            error = data[7:]
        elif data == "[DONE]":
            break
    return time.perf_counter() - start, last_metrics, error


def percentile(sorted_latencies: list[float], p: float) -> float:
    """线性插值分位数（小样本下比取整下标更稳）。"""
    if len(sorted_latencies) == 1:
        return sorted_latencies[0]
    k = (len(sorted_latencies) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_latencies) - 1)
    return sorted_latencies[f] + (sorted_latencies[c] - sorted_latencies[f]) * (k - f)


def main():
    parser = argparse.ArgumentParser(description="InsightForge AI 性能基准")
    parser.add_argument("--base-url", default="http://localhost:8502")
    parser.add_argument("--iterations", type=int, default=5, help="每类查询的重复次数")
    parser.add_argument("--password", default=f"bench_{uuid.uuid4().hex[:12]}")
    args = parser.parse_args()

    print(f"🚀 性能基准 (base_url={args.base_url}, iterations={args.iterations})")
    token = ensure_auth(args.base_url, args.password)
    print("  bench 用户就绪")
    ensure_dataset(args.base_url, token)

    latencies: list[float] = []
    failures: list[str] = []
    total_in = total_out = 0
    cost = 0.0
    seq = 0
    for query in QUERIES:
        for _ in range(args.iterations):
            seq += 1
            latency, metrics, error = run_one(args.base_url, token, query)
            latencies.append(latency)
            status = f"{latency:.2f}s" if not error else f"FAIL ({error[:40]})"
            print(f"  [{seq:2d}] {query[:18]:<20} {status}")
            if error:
                failures.append(query)
            # 每轮独立新会话：取该轮最后一条 METRICS（本轮累计），跨轮求和
            if metrics:
                total_in += metrics.get("input_tokens", 0)
                total_out += metrics.get("output_tokens", 0)
                cost += metrics.get("cost_cny", 0)

    ok_latencies = sorted(latencies)
    mean = statistics.mean(latencies)
    result = {
        "base_url": args.base_url,
        "iterations": args.iterations,
        "queries": len(QUERIES),
        "samples": len(latencies),
        "failures": len(failures),
        "p50_s": round(percentile(ok_latencies, 0.50), 2),
        "p95_s": round(percentile(ok_latencies, 0.95), 2),
        "p99_s": round(percentile(ok_latencies, 0.99), 2),
        "mean_s": round(mean, 2),
        "token_input": total_in,
        "token_output": total_out,
        "cost_cny": round(cost, 4),
    }

    print("\n📊 性能结果:")
    print(f"  样本数: {result['samples']}（失败 {result['failures']}）")
    print(f"  P50: {result['p50_s']}s   P95: {result['p95_s']}s   P99: {result['p99_s']}s")
    print(f"  平均: {result['mean_s']}s")
    print(f"  Token: 输入 {total_in} / 输出 {total_out}，估算成本 ¥{result['cost_cny']}")

    out_file = Path(__file__).resolve().parent.parent / "agent" / "logs" / "benchmark_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {out_file}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
