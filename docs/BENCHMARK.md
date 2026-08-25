# 性能基准与 RAG 评估说明

两个评估入口：**端到端性能基准**（`scripts/benchmark.py`）与 **RAG 质量评估**（`tests/rag_eval/`）。

---

## 1. 性能基准（scripts/benchmark.py）

### 1.1 测什么

单用户端到端延迟：从 `POST /api/chat` 发出到 SSE 流收到 `[DONE]` 的**全程耗时**
（含 LLM 思考、工具调用、规划执行、流式渲染），并顺带采集 `[METRICS]` 事件中的 Token/成本。

### 1.2 怎么跑

前置：服务已启动（本地或 Docker 均可）。

```bash
python scripts/benchmark.py                          # 默认 http://localhost:8502 × 5 轮
python scripts/benchmark.py --iterations 3 --base-url http://localhost:8502
```

脚本自动完成：注册临时 bench 用户 → 检查数据集（无则生成 200 行样例销售 CSV 并上传）→
5 类查询（计数/趋势/对比/异常/总结）× N 轮 → 统计输出并落盘 `logs/benchmark_results.json`。

### 1.3 结果解读

```text
📊 性能结果:
  样本数: 25（失败 0）
  P50: 6.8s   P95: 11.2s   P99: 12.4s
  平均: 7.5s
  Token: 输入 48210 / 输出 9120，估算成本 ¥0.0619
```

- **P50/P95/P99**：线性插值分位数。注意：本系统的查询会触发完整分析流水线
  （LLM 多轮 + SQL + 图表），秒级延迟是 LLM 应用常态；对比时应固定模型、数据集与问题集。
- **失败样本**：SSE 流中出现 `[ERROR]` 计为失败，脚本退出码 1。
- **多轮对比**：结果 JSON 含时间戳前的 `base_url/iterations` 元数据，多次运行可直接 diff
  观察 prompt/模型/参数调整带来的变化。
- Token 为**会话累计值**（服务端按 session 聚合），基准脚本每轮独立 session，
  最终值即全部轮次总和。

### 1.4 方法学边界

- 单用户串行：反映交互延迟，不反映并发吞吐（如需压测可自行改造为 locust/并发脚本）。
- 网络路径真实（HTTP + SSE），含浏览器可感知的全部服务端时间，不含前端渲染时间。

---

## 2. RAG 质量评估（tests/rag_eval/）

### 2.1 设计

- **受控语料**：`eval_knowledge.md`（虚构产品「云帆 CRM」，20+ 条事实边界清晰）
- **用例**：20 条事实型问答（`test_cases.py`：question / ground_truth / keywords）
- **隔离**：独立 collection + 临时持久化目录（沿用 `test_vector_store_isolation.py`
  的 `__new__` 注入惯例），真实跑「查询改写 → 向量召回 → rerank 精排 → 生成」全链路，
  不污染真实知识库
- **门控**：`rag_eval` marker 默认排除（`pytest.ini`），显式运行才执行，
  存量 178 个测试与无 Key 的 CI 不受影响

### 2.2 两层评估

| 层 | 脚本 | 依赖 | 指标 | 阈值 |
|----|------|------|------|------|
| 检索层（确定性） | `test_rag_retrieval_hit.py` | 仅 DashScope（embed+rerank） | 关键词命中率 / 空检索 | ≥ 80%（实测 **100%**，20/20） |
| 端到端（LLM judge） | `test_rag_quality.py` | + `requirements-eval.txt`（ragas） | faithfulness / answer_relevancy / context_precision | ≥ 0.80 / 0.85 / 0.75 |

```bash
# 快速回归（推荐日常，仓库根直跑）
python -m pytest tests/rag_eval/test_rag_retrieval_hit.py -m rag_eval -v

# ragas 全量（先装评估依赖）
pip install -r requirements-eval.txt
python -m pytest tests/rag_eval/test_rag_quality.py -m rag_eval -v -s
```

### 2.3 版本说明（重要）

ragas **0.1.x 依赖老版 langchain API，与本项目 langchain 1.3 冲突**，因此评估依赖
单独放 `requirements-eval.txt`（ragas 0.2.x，`LangchainLLMWrapper` 包装项目模型），
不进主 requirements，避免污染运行时。未安装 ragas 时 `test_rag_quality.py` 自动 skip。

### 2.4 未达标时调什么

| 指标低 | 方向 |
|--------|------|
| 检索命中率 | 调大 `retrieve_k`（粗召回池）、检查 chunk_size（config/rag.yml）、确认语料已入库 |
| context_precision | 提高 `rerank_score_threshold`、减小 `rerank_top_n` |
| faithfulness | 优化 RAG prompt（要求仅依据参考资料作答）、检查 chunk 截断是否割裂事实 |
| answer_relevancy | 检查问题改写（RetrievalQueryRewriter）是否偏离原意 |
