# 实施计划：头像移除 + 图标美化 + 思考进度 + 画图 Agent 改进

日期：2026-07-23 · 分支：feat/welcome-page-ui

四项需求彼此独立，分四个工作流推进。所有前端沿用既有 sci-tech / Cobalt 设计语言（tokens.css），不引入新依赖。

---

## 工作流 1：取消用户头像功能

> 移除「用户上传头像」功能；保留聊天消息气泡上的 `.avatar` 图标（仅类名撞车，属工作流 2）。nickname / 密码等共用逻辑保留。

### 后端 `agent/api/fastapi_server.py`
- 删除 `_avatar_web_url()`（856–867）
- `/api/me`（271–279）：删除 `"avatar_path": user.get("avatar_path")` 行（278）
- `GET /api/profile`（870–880）：删除 `"avatar_url": _avatar_web_url(...)`（879），保留 account/nickname
- 删除 `POST /api/avatar` 端点整体（900–916）
- 删除 `/avatars` 静态挂载与目录创建（1122–1125：`_avatars_dir`、`os.makedirs`、`app.mount`）

### DB `agent/database/user_db.py`
- 迁移块（90–92）：删除 `self._ensure_column(conn, "users", "avatar_path", "TEXT")`（92）；保留 nickname 迁移
- `validate_token`（196–223）：SELECT 去掉 `u.avatar_path`（203），返回 dict 去掉 `"avatar_path"`（223）
- `get_user`（231–238）：SELECT 去掉 `avatar_path`（235）
- `update_profile`（240–258）：删除 `avatar_path` 参数与分支（241、247–249），仅保留 nickname
- 已有库里的 `avatar_path` 列保持休眠（不 DROP，避免 SQLite 版本风险）；新库不再创建该列

### 前端 `agent/api/static/app.html`
- 删除 profile 弹窗中的 `.profile-avatar-row` 整块（238–244），保留昵称/账号/密码字段与 `.modal-divider`

### 前端 `agent/api/static/js/app.js`
- `loadProfile`（15–29）：删除 `avatar_url` 分支（22–27），统一用新 SVG 用户图标 + 昵称（见工作流 2）
- `openProfileModal`（31–53）：删除头像相关行（40–42、48、49–52），保留昵称/账号/密码填充
- 删除 `uploadAvatar` 函数整体（59–82）
- 第 11 行 `'👤 ' + accountName` 占位改为新 SVG 用户图标 + 名字

### 前端 `agent/api/static/css/app.css`
- 删除 `.user-info .avatar`（98–103）
- 删除 profile 头像样式：`.profile-avatar-row`、`.avatar-lg`、`.avatar-lg:not([src])`、`.avatar-upload`、`.avatar-hint`、`.avatar-upload input[type=file]`（1184–1206）；**保留 `.modal-divider`**（1207–1211，与密码区分隔线共用）
- 保留 `.message .avatar`（298–316）——聊天气泡图标，工作流 2 复用

### 测试
- `agent/tests/test_auth.py`：确认 `test_update_profile_changes_nickname`（91–107）未涉及头像（探查确认仅测 nickname）；如存在 avatar 断言则移除

---

## 工作流 2：自定义内联 SVG 图标（聊天 + 登录统一）

> 用与落地页 feature-card 同风格的线性 SVG 替换 emoji 🤖/👤。机器人图标同时用于聊天气泡与登录弹窗标题。

### 新增 `agent/api/static/js/icons.js`
暴露 `window.Icons = { bot, user }`，两个 SVG 字符串（`viewBox="0 0 28 28"` `fill="none" stroke="currentColor" stroke-width="1.6"`，几何线性）：
- `bot`：机器人头部——圆角方头 + 双眼 + 顶部天线/节点，技术感
- `user`：人物——圆头 + 肩部弧线，简洁
- 两图在 app.html 与 index.html 均加载，避免重复定义

### `app.html`
- `<head>` 增加 `<script src="/static/js/icons.js"></script>`（在 auth.js/app.js 之前）

### `index.html`
- `<head>` 增加 `<script src="/static/js/icons.js"></script>`（在 landing.js 之前）

### `agent/api/static/js/app.js`
- `appendMessage`（591）：`'👤'`→`window.Icons.user`，`'🤖'`→`window.Icons.bot`
- `loadProfile` 与第 11 行：`'👤 '`→`window.Icons.user + ' '`

### `agent/api/static/js/landing.js`
- `LOGIN_TEXT`/`REGISTER_TEXT` 的 `title` 去掉 `🤖 ` 前缀，改为纯 `登录`/`注册`（23–26）
- `setMode`（34）：`titleEl.textContent = t.title` → `titleEl.innerHTML = window.Icons.bot + '<span>' + t.title + '</span>'`

### `agent/api/static/css/app.css`
- `.avatar svg { width: 18px; height: 18px; }`（继承 `.message.user/.assistant .avatar` 的 color，SVG 用 currentColor）
- `.user-info svg { width: 16px; height: 16px; flex-shrink: 0; }`

### `agent/api/static/css/auth.css`
- `#auth-title svg { width: 24px; height: 24px; vertical-align: -5px; margin-right: 10px; color: var(--color-accent); }`

---

## 工作流 3：步骤清单式思考进度（"AI is working" + 当前步骤）

> 复杂分析期间显示完整步骤清单并标注当前步。复用已存在但未接通的 `PlannerAgent` 步骤事件，经新 SSE token `[STEP:json]` 下发到前端。

### 核心架构（解决"工具阻塞期间无进度"）
`run_full_analysis` 在 SSE 后台线程内同步执行 `PlannerAgent.run()`，期间 ReactAgent 的流式循环被阻塞、不 yield。因此步骤事件须**绕过 ReactAgent 的 yield**，直接注入 `_stream_with_heartbeat` 的 asyncio.Queue。

### 新增 `agent/utils/progress_emitter.py`
```python
class ProgressEmitter:
    def bind(self, loop, async_queue): ...        # 绑定主协程 loop+queue
    def emit(self, event_type, data): ...          # loop.call_soon_threadsafe 线程安全推 ("progress",(type,data))
    def close(self): ...
# contextvar：set_progress_emitter / get_progress_emitter
```

### `agent/api/fastapi_server.py`
- `_stream_with_heartbeat`（32–74）：
  - 增加 `progress_emitter=None` 入参；创建后 `progress_emitter.bind(loop, queue)`
  - yield 契约由 `(is_heartbeat, value)` 改为 `(kind, value)`，kind ∈ `{"heartbeat","chunk","progress"}`
  - 心跳文本由 `data: [THINKING]正在分析...\n\n` 改为 `data: [KEEPALIVE]\n\n`（纯保活，不再覆盖状态文案）
- `api_chat.generate()`（332–398）：
  - 创建 `emitter = ProgressEmitter()`，传入 `_stream_with_heartbeat(..., progress_emitter=emitter)` 与 `agent.execute_stream(..., progress_emitter=emitter)`
  - 消费 `("heartbeat",_)` → `yield "data: [KEEPALIVE]\n\n"`
  - 消费 `("progress",(type,data))` → `yield f"data: [STEP:{json.dumps({'type':type,**data}, ensure_ascii=False)}]\n\n"`
  - `("chunk",v)` 走原有 `[THINKING]` 透传 / 句子拆分逻辑

### `agent/agent/react_agent.py`
- `execute_stream`（47–56）增加 `progress_emitter=None` 入参；在 `set_request_context` 旁调用 `set_progress_emitter(progress_emitter)`（同后台线程，contextvar 可见）
- 保留 `local_tool_names` 的 `[THINKING]` 状态（单工具场景仍用）；`run_full_analysis` 项可保留或精简

### `agent/agents/planner_agent.py`
- `run()`（157–240）插入进度发射（用 `get_progress_emitter()`，None 时 no-op）：
  - 计划生成后：`emit("plan", {"title":title, "steps":[{step,agent,task,label} for ...]})`
  - 每步前：`emit("step_start", {"step":n})`
  - 每步后（含失败）：`emit("step_done", {"step":n})`
  - 可选：`emit("status", {"text": f"步骤 {n}: {task}"})`
- 新增 `AGENT_LABELS = {"sql_query":"SQL 查询","trend_analysis":"趋势分析","product_analysis":"产品分析","risk_analysis":"风险分析","visualization":"图表生成","report":"生成报告","export":"导出报告"}`，plan 事件带 label
- `run_full_analysis`（`agent_tools.py:90`）调用 `analyst.run({...})` 不变；进度经 contextvar 自动到达

### 前端 `agent/api/static/js/app.js` · `processLine`（444–523）
- 新增 `[KEEPALIVE]` 分支：仅 `return false`（不渲染；读循环已 `resetIdle`）
- 新增 `[STEP:` 分支：
  - 解析 JSON；`type==="plan"` → 构建 `.step-progress` 清单（所有步骤 ○ 待执行），插入 `.chat-status` 内并显示
  - `type==="step_start"` → 标记对应步骤为 ● 进行中（加 spinner）
  - `type==="step_done"` → 标记 ✓ 完成
  - `type==="status"` → 更新 `.status-text`
- `thinking=false` 转内容时：若 `.chat-status` 含 `.step-progress` 则**不隐藏**（保留为记录），否则照旧隐藏
- `[THINKING]` 分支保留：无清单时更新 `.status-text`；有清单时仅更新状态行不覆盖清单

### 前端 `agent/api/static/css/app.css`
- `.step-progress`：纵向清单，mono 字体、`--text-xs`、`--color-ink-3`
- `.step-progress .step` 三态：`.pending`(○ `--color-rule-2`)、`.active`(● + spinner + `--color-accent`)、`.done`(✓ `--color-accent-2`)
- 复用已有 `.chat-status .spinner`

---

## 工作流 4：画图 Agent 混合方案（轴范围/标签/刻度/配色 数据驱动）

> Python 按数据确定性计算（刻度格式、y 轴范围、分类轴、配色）；LLM 负责人类可读轴标签与图表语义。

### `agent/visualization/charts.py`（核心改动）
新增静态工具方法：
- `_series_stats(series)` → {min,max,max_abs,has_neg,is_int_like,nunique}
- `_tick_format(max_abs)` → max_abs≥1000 用 `.2~s`（SI: k/M/G）；≥10 整数 `,.0f`；否则 `,.2f`
- `_y_range(series, start_zero=False)` → `[lo,hi]` 带 5% padding；`start_zero=True`（柱状图）时 lo=0
- `_is_low_cardinality(series, n=24)` → x 轴是否转 `type="category"`

应用进各 builder：
- `line_chart`：y 轴 `update_yaxes(tickformat=fmt, range=y_range)`；x 低基数/月型→`type="category"`；`update_traces(line_color=PALETTE[0])`；接收并应用 `x_label`/`y_label`
- `bar_chart`：y 轴 `range=[0, max*1.1]`、`tickformat=fmt`；`marker_color=PALETTE[0]`；新增 `x_label`/`y_label` 形参
- `pie_chart`：`update_traces(marker=dict(colors=PALETTE))`；标签可读化
- `scatter_chart`：`colorway=PALETTE`；轴范围/刻度同 line
- `heatmap`：保留 `RdBu_r`，`height` 按行列数自适应（min 400 / max 700）
- 统一 `colorway=PALETTE`、`height=500`（heatmap 除外）
- `PALETTE = ['#3b82f6','#22d3ee','#a78bfa','#34d399','#fbbf24','#fb7185']`（cobalt 系，与主题一致）

### `agent/agents/visualization_agent.py`
- `CHART_DECISION_PROMPT`（27–51）扩展：
  - 喂给 LLM 的数据摘要改为**含统计量**（每列 dtype + 数值列 min/max/mean/nunique；类别列 nunique+top3 值）
  - 输出字段增加 `x_label`、`y_label`（人类可读中文轴标签，依列语义，如 total_revenue→"总营收(元)"）；保留 chart_type/title/x_col/y_col/reason
  - 明确：轴范围/刻度由系统按数据自动处理，LLM 只需给标签
- `_decide_charts`（202–229）：构建含统计量的 `summary`；解析时保留 `x_label`/`y_label`
- `_generate_chart`（156–200）：将 `x_label`/`y_label`/`title` 透传给 `ChartGenerator` 各方法；`validated` 增加 `x_label`/`y_label`

---

## 测试与验证
- `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent`
- `cd agent && python -m pytest tests/ -v`（确保 test_auth 等通过）
- 启动服务 `python -m api.fastapi_server`，浏览器验证：
  1. 头像：profile 弹窗无头像行；上传入口消失；昵称/改密正常；`/api/me`、`/api/profile` 无 avatar 字段
  2. 图标：聊天双方头像为线性 SVG；登录弹窗标题为机器人 SVG + 文字
  3. 进度：发起"分析各月销售趋势并生成报告"，观察步骤清单逐项点亮、`[KEEPALIVE]` 不再覆盖文案
  4. 图表：对含大数值（百万级）与月份列的数据集生成图表，确认 y 轴 SI 刻度、合理范围、人类可读标签、cobalt 配色
- Windows 杀旧服务用 `taskkill //F //PID`（见 memory）

## 风险与回滚
- `validate_token` 是鉴权热路径：移除 `avatar_path` 后跑全量 auth 测试
- `_stream_with_heartbeat` yield 契约变更：仅 `api_chat.generate()` 一处消费，同步更新
- contextvar 在后台线程内 set（非跨线程传播），`PlannerAgent.run` 与 `execute_stream` 同线程，可见性正确
- 进度 emitter 在非流式路径（`/api/analysis`，前端未用）为 None，`run()` no-op，无影响
