# 测试

- **规模**：20 个文件、139 个用例，全量通过、全离线（约 15s）。
- **mock 策略**：LLM 与外部服务 100% mock；`test_sql_sandbox.py` 纯函数无 mock；涉及 DB 的用 temp SQLite / 临时 DuckDB。
- **覆盖重点**：SQL 沙箱 AST 守卫（31 例）、鉴权与重定向循环修复、多用户隔离（数据集 / 设置 / 文件 / 客户档案）、配置优先级、模型缓存热重载、RAG 格式化、文档报告截断。
- **运行器**：pytest（`python -m unittest discover tests` 只能收集 10 个 `unittest.TestCase` 文件，会漏掉 10 个 pytest 函数式测试文件，**请用 pytest**）。

```bash
conda activate AnalysisAgent
cd agent
python -m pytest tests/ -v
```
