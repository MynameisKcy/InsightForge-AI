项目运行仅限于：C:\Users\user\.conda\envs\RAG\python.exe
项目改动仅限于agent文件夹目录下
你当前目标功能包括：



\* SQL 查询

\* 趋势分析

\* 产品分析

\* 图表生成

\* 自动报告

\* Word/PDF 导出

\* Markdown 输出



agent之间的交互格式使用json或message

这是一个标准的：



```

AI Data Analyst Multi-Agent System

```



下面我给你一个：



\# 真正合理的 Agent 架构（推荐版本）



\---



\# 一、推荐 Agent 总数



我建议：



\# 第一阶段：



\## 6 个核心 Agent



这是最合理的数量。



\---



\# 最终推荐架构



```text id="4jtl6n"

User

&#x20;↓

Planner Agent

&#x20;↓

SQL Agent

&#x20;↓

Analysis Agent

&#x20;   ├── Trend Analysis

&#x20;   ├── Product Analysis

&#x20;   ├── Risk Analysis

&#x20;↓

Visualization Agent

&#x20;↓

Report Agent

&#x20;↓

Export Agent

```



\---



\# 二、每个 Agent 的职责（核心）



\---



\# 1. Planner Agent（任务规划 Agent）



这是整个系统的大脑。



\---



\# 核心职责



负责：



```text id="uh8jvb"

理解用户需求

拆解分析任务

规划执行流程

决定调用哪些Agent

```



\---



\# 示例



用户：



> 分析最近半年利润下降原因并生成报告



\---



\# Planner Agent 输出：



```text id="n9iqgk"

1\. 查询销售与利润数据

2\. 进行趋势分析

3\. 进行产品利润分析

4\. 进行区域风险分析

5\. 生成图表

6\. 输出最终报告

```



\---



\# 输入



```python id="vxqg6x"

用户自然语言问题

```



\---



\# 输出



```python id="lm7a4e"

任务列表

Agent执行顺序

```



\---



\# 推荐技术



```text id="wx0c5l"

CrewAI

LangGraph

```



\---



\# 三、SQL Agent（数据查询 Agent）



这是核心数据入口。



\---



\# 核心职责



负责：



```text id="cdkqz8"

自然语言转SQL

SQL执行

返回DataFrame

```



\---



\# 工作流



```text id="q7k4my"

用户问题

↓

LLM生成SQL

↓

DuckDB执行

↓

返回结果

```



\---



\# 示例



用户：



> 找出利润最低的产品类别



\---



\# SQL Agent 自动生成：



SELECT\\ category,\\ SUM(profit)\\ FROM\\ sales\\ GROUP\\ BY\\ category\\ ORDER\\ BY\\ SUM(profit)\\ ASC



\---



\# 输入



```python id="1xhq5t"

任务描述

数据库Schema

```



\---



\# 输出



```python id="5mmjgt"

Pandas DataFrame

```



\---



\# 推荐技术



```python id="p7k2w4"

DuckDB

SQLAlchemy

LangChain SQL Agent

```



\---



\# 四、Analysis Agent（数据分析 Agent）



这是整个系统最重要的 Agent。



\---



\# 它应该再拆成：



| 子模块              | 功能   |

| ---------------- | ---- |

| Trend Analysis   | 趋势分析 |

| Product Analysis | 产品分析 |

| Risk Analysis    | 风险分析 |



\---



\# 4.1 Trend Analysis Agent



\---



\# 职责



负责：



```text id="jlwm5e"

销售趋势

利润趋势

同比环比

增长率

时间序列分析

```



\---



\# 分析内容



\---



\## 月销售趋势



\## 月利润趋势



\## 增长率分析



\## 峰值/低谷分析



\## 异常波动分析



\---



\# 输出



```python id="gd42u5"

{

&#x20;   "trend\_summary": "...",

&#x20;   "growth\_rate": "...",

&#x20;   "anomaly\_month": "..."

}

```



\---



\# 推荐图（非常关键）



\## 趋势折线图



例如：



\---



\# 推荐技术



```python id="qz9tp5"

pandas

numpy

statsmodels

```



\---



\# 4.2 Product Analysis Agent



这是商业分析核心。



\---



\# 职责



负责：



```text id="c6j63n"

产品销量分析

产品利润分析

Top产品分析

低利润产品分析

类别分析

```



\---



\# 输出



```python id="dzslxy"

{

&#x20;   "top\_products": \[],

&#x20;   "low\_profit\_products": \[],

&#x20;   "category\_summary": \[]

}

```



\---



\# 典型分析



\---



\## TOP产品



\## 利润最低产品



\## 类别贡献分析



\## 高销量低利润产品



\---



\# 推荐图



\## TOP产品柱状图



\---



\# 4.3 Risk Analysis Agent（非常推荐）



这是你最有竞争力的地方。



\---



\# 职责



负责：



```text id="ow8tiz"

异常订单检测

利润异常

销量异常

区域异常

```



\---



\# 可以分析



\---



\## 哪个月利润异常下降



\## 哪个区域订单异常减少



\## 哪类产品亏损严重



\---





\# 五、Visualization Agent（图表生成 Agent）



这是“高级感”来源。



\---



\# 职责



负责：



```text id="88x6y4"

自动生成图表

图表选择

图表布局

```



\---



\# 需要支持



| 图表     | 用途   |

| ------ | ---- |

| 趋势图    | 时间趋势 |

| TOP产品图 | 排名分析 |

| 热力图    | 区域分析 |

| 饼图     | 类别占比 |

| 散点图    | 利润关系 |



\---



\# 推荐技术



\---



\## Plotly（强烈推荐）



原因：



```text id="jlwm92"

交互式

适合Web

适合Streamlit

```



\---



\# 图表生成逻辑



\---



\## 自动判断图表类型



例如：



| 数据类型 | 图表  |

| ---- | --- |

| 时间序列 | 折线图 |

| TopK | 柱状图 |

| 占比   | 饼图  |

| 区域矩阵 | 热力图 |



\---



\# 六、Report Agent（报告生成 Agent）



这是最终输出核心。



\---



\# 职责



负责：



```text id="rzv8nn"

整合所有分析结果

生成Markdown

生成商业分析报告

```



\---



\# 最终输出格式



你要求的：



```markdown id="wccjlwm"

\# 销售分析报告



\## 总体趋势



销售额整体增长 8%



\## 利润分析



利润在 3 月开始下降



\## 产品分析



家具类亏损严重



\## 风险分析



西部地区订单异常减少



\## 建议



优化家具供应链

```



\---



\# 它应该自动整合：



\---



\## SQL结果



\## 趋势分析



\## 产品分析



\## 风险分析



\## 图表路径



\---



\# 推荐：



\## Jinja2 模板化报告



\---



\# 七、Export Agent（导出 Agent）



这个建议单独拆。



\---



\# 职责



负责：



```text id="9q4nfm"

Markdown导出

Word导出

PDF导出

HTML导出

```



\---



\# 为什么单独拆



因为：



```text id="2l8j33"

格式转换

文件生成

与分析逻辑解耦

```



\---



\# 推荐技术



| 格式       | 技术          |

| -------- | ----------- |

| Word     | python-docx |

| PDF      | reportlab   |

| Markdown | 原生          |

| HTML     | Jinja2      |



\---



\# 八、最终 Agent 协作流程（非常重要）



\---



\# 最终工作流



```text id="qt2bdj"

用户问题

&#x20;   ↓

Planner Agent

&#x20;   ↓

SQL Agent

&#x20;   ↓

Trend Analysis Agent

&#x20;   ↓

Product Analysis Agent

&#x20;   ↓

Risk Analysis Agent

&#x20;   ↓

Visualization Agent

&#x20;   ↓

Report Agent

&#x20;   ↓

Export Agent

```



\---



\# 九、推荐项目目录结构（真正工程化）

根据原项目内容进行改动

project/

│

├── agents/

│   ├── planner\_agent.py

│   ├── sql\_agent.py

│   ├── trend\_agent.py

│   ├── product\_agent.py

│   ├── risk\_agent.py

│   ├── visualization\_agent.py

│   ├── report\_agent.py

│   └── export\_agent.py

│

├── database/

│   ├── duckdb\_manager.py

│   └── schema\_loader.py

│

├── analysis/

│   ├── trend\_analysis.py

│   ├── product\_analysis.py

│   ├── anomaly\_detection.py

│   └── statistics.py

│

├── visualization/

│   ├── line\_chart.py

│   ├── heatmap.py

│   ├── bar\_chart.py

│   └── pie\_chart.py

│

├── reports/

│   ├── markdown/

│   ├── pdf/

│   └── word/

│

├── templates/

│   └── report\_template.md

│

├── api/

│   └── fastapi\_server.py

│

├── frontend/

│   └── streamlit\_app.py

│

└── data/

```



\---



\# 十、你现在真正应该优先做的顺序（关键）



不要直接上 CrewAI 全家桶。



\---



\# 第一阶段（必须）



先实现：



\---



\## SQL Agent



\---



\## Trend Analysis Agent



\---



\## Visualization Agent



\---



\## Report Agent



\---



\# 第二阶段



再加入：



\---



\## Product Analysis Agent



\---



\## Risk Analysis Agent



\---



\# 第三阶段



再加入：



\---



\## Planner Agent



\---



\## CrewAI Workflow



\---



\## Memory





