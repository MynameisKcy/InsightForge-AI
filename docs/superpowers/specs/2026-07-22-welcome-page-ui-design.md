# 欢迎页 + 主应用 UI 重塑设计规范

- **日期**：2026-07-22
- **分支**：`feat/welcome-page-ui`（从 `feat/config-file-report` 拉出）
- **状态**：待审阅

## 1. 背景与目标

InsightForge AI 现有前端是纯手写 CSS + 原生 JS，全部 HTML 嵌在 `agent/api/fastapi_server.py`（3216 行）的两个字符串常量里：`LOGIN_PAGE`（`GET /`）和 `HTML_TEMPLATE`（`GET /app`）。认证已完整存在（bcrypt + bearer token 24h + `user_db` 的 `users`/`sessions` 表），但鉴权是"前端 JS 挡一下 + 部分 API 拦截"，`GET /app` 服务端不校验 token，无独立落地页。

本次目标：
1. 安装 Hallmark 设计技能（`~/.claude/skills/hallmark/`），用它重塑 UI，拒绝 AI-slop 视觉。
2. 新建欢迎落地页，介绍项目功能，登录/注册入口内嵌其上。
3. 全站服务端强制鉴权，做到"不登录无法使用"。
4. 把前端从 Python 字符串抽成静态文件 + FastAPI StaticFiles。

## 2. 已确认决策

| 维度 | 决策 |
|---|---|
| 范围 | 欢迎页 + 主应用全面重塑 |
| 主应用视觉 | 完全交给 Hallmark redesign（按"多智能体数据分析平台"brief 自选主题） |
| 配色 | **科技风**：冷色高对比、电光蓝强调、深空或冷白底、几何无衬线、克制锐利 |
| 欢迎页与登录 | 落地页内嵌登录入口，模态弹窗 |
| 鉴权强度 | 全站服务端强制鉴权（依赖 + `GET /app` 重定向） |
| 登录态持久 | "记住我"开关（localStorage / sessionStorage），后端 24h 不变 |
| Hallmark 安装 | 全局 `~/.claude/skills/hallmark/`（`npx skills add nutlope/hallmark`） |
| 前端工程化 | 抽静态文件 + StaticFiles |
| 架构方案 | A+B 融合（API 层 401 + `GET /app` 服务端校验重定向） |
| 登录形态 | 模态弹窗 |
| 新增端点 | `GET /api/me`（返回 user_id/account/nickname/avatar_path） |
| 鉴权工程优化 | 统一 `require_auth` 依赖 + 统一 `auth.js` fetch 封装 + `validate_token` 进程内 LRU 短缓存 |
| 参考页处理 | hum-07 仅作落地页宏观结构参考，配色/字体/质感按科技风 brief 由 Hallmark 自选，不照搬温暖风 |

## 3. 整体架构

### 3.1 静态文件结构

```
agent/static/
├── index.html          # 欢迎落地页（Hallmark redesign 产物）
├── app.html            # 主应用（Hallmark redesign 产物，HTML_TEMPLATE 重塑）
├── css/
│   ├── tokens.css      # 全站设计 token（科技风）
│   ├── landing.css     # 落地页专用
│   ├── app.css         # 主应用专用
│   └── auth.css        # 登录/注册弹窗
└── js/
    ├── auth.js         # 共享：token 存取、记我、401 拦截跳转、统一 fetch
    ├── landing.js      # 落地页交互（弹窗、表单切换、提交、滚动）
    └── app.js          # 主应用现有原生 JS 迁移（SSE/会话/数据集/侧栏）
```

### 3.2 路由变化

| 路由 | 改造后 | 鉴权 |
|---|---|---|
| `GET /` | 返回 `index.html` | 公开 |
| `GET /app` | 返回 `app.html`，**服务端校验 token，无则 302 → `/`** | 强制（重定向） |
| `GET /static/*` | StaticFiles 挂载 | 公开 |
| `POST /api/register` `/api/login` | 保留 | 公开 |
| `POST /api/logout` | 保留 | 需 token |
| `GET /api/me` | **新增**，返回当前用户 | 需 token |
| `POST /api/chat` 及其余 `/api/*` 业务端点 | 全挂 `Depends(require_auth)` | 强制（401） |

### 3.3 鉴权实现（`agent/api/auth.py`）

```python
# 伪代码示意
_token_cache = {}  # token -> (user, expires_at)，进程内短缓存

def require_auth(request: Request) -> dict:
    token = _extract_bearer(request)
    user = _validate_token_cached(token)   # LRU 短缓存命中则不查库
    if not user:
        raise HTTPException(401, "未登录或会话已过期")
    return {"user_id": user["id"], "account": ..., "nickname": ...}
```

- 业务 API 全挂 `Depends(require_auth)`：`/api/chat`、`/api/datasets*`、`/api/datasources/*`、`/api/profile`、`/api/avatar`、`/api/password`、`/api/sessions*`、`/api/knowledge*` 等。
- 公开 API：`/api/register`、`/api/login`。
- `GET /api/me` 用 `require_auth`，返回 `{user_id, account, nickname, avatar_path}`。
- `GET /app` 路由内 `validate_token`，失败 `RedirectResponse("/", 302)`；`/app` 改成显式路由（不依赖 StaticFiles 自动托管），校验通过后 `FileResponse(app.html)`。
- `validate_token` 加进程内 LRU 短缓存（几十秒），token 24h 有效，短缓存安全；命中不查 SQLite `sessions` 表，鉴权变 O(1) 内存查表。
- 现有 `_get_user_id(request)` 匿名返回 `"anonymous"`：保留给极少数确实允许匿名探测的端点（如有）；业务端点统一改用 `require_auth`。`POST /api/chat` 现有 anonymous→401 显式分支被 `require_auth` 统一覆盖，可移除。

### 3.4 "记住我"实现

- 登录/注册弹窗提交时读"记住我"复选框。
- 勾选 → `localStorage.setItem('token', ...)`；不勾 → `sessionStorage.setItem(...)`。
- `js/auth.js` 取 token：`localStorage.getItem('token') ?? sessionStorage.getItem('token')`。
- 后端 token 24h 有效不变。

## 4. Hallmark 设计语言

### 4.1 安装

`npx skills add nutlope/hallmark` → `~/.claude/skills/hallmark/`（`SKILL.md` + `references/`）。重装即更新。

### 4.2 设计 DNA 流程

1. **`hallmark study` hum-07**：提取该页宏观结构、字体配对、色彩锚点，输出可移植 `design.md`。仅作落地页宏观结构参考，**拒绝像素级克隆**。
2. **`hallmark redesign <现有主应用>`**：按本项目 brief 重新选主题，跑 57 个 slop-test 门控 + 预发射自我批评后交付。

### 4.3 Hallmark brief（科技风约束）

> 多智能体协同数据分析平台（InsightForge AI）：LangChain+LangGraph 编排，自然语言驱动 SQL/趋势/产品/风险分析、图表生成、多格式报告导出；用户是数据分析师；视觉需严肃、可信、信息密集但克制；**配色科技风——冷色高对比、电光蓝强调、深空或冷白底、几何无衬线、锐利描边、低圆角**。

主题倾向：Cobalt 类（冷蓝/电光蓝、深空底）或 Lumen 类（AI 推理工具冷峻发光质感）；**排除 Hum 温暖手作风**。

### 4.4 科技风设计 token 预期（落到 `tokens.css`，最终值由 Hallmark 主题确定）

| token 类 | 科技风预期 |
|---|---|
| 背景 | 深空底（近黑/深蓝灰，如 `#0a0e1a`/`#0f1729`）或冷白底（`#f8fafc`）二选一 |
| 强调色 | 电光蓝/青蓝（`#3b82f6`/`#06b6d4` 类），替代现有玫红 `#e94560` |
| 中性色 | 冷灰阶（slate/zinc），非暖灰 |
| 字体 | 几何无衬线（Inter/Geist 类）或 Hallmark 自选冷峻配对；等宽 JetBrains Mono/SF Mono |
| 数据呈现 | 表格/代码块深底高亮，图表配色与 token 协同 |
| 质感 | 克制发光/边框描边、锐利阴影、低圆角 |

### 4.5 两页视觉关系

落地页与主应用共享同一套科技风 token；两页像同一站点，不是换皮。

## 5. 落地页结构与登录交互（`index.html`）

### 5.1 宏观结构（参考 hum-07 落地页骨架，套科技风）

1. **顶部导航条**：左 Logo「InsightForge AI」+ 右「登录」+「免费开始」。已登录态（token 有效）该处变「进入工作台 →」直接跳 `/app`。
2. **Hero 区**：主标题 + 副标题 + 双 CTA（「立即开始」「查看示例」）。定位：多智能体协同数据分析平台。科技风深空/网格质感背景。
3. **功能特性区**：3–4 卡片 ——
   - 自然语言驱动（Smart Assistant 智能客服 + ReAct 13 工具）
   - 多智能体分析流水线（PlannerAgent 编排：SQL→趋势/产品/风险→可视化→报告→导出）
   - 多源数据管理（CSV/Excel 上传 + MySQL/PostgreSQL 预配置 + DuckDB 统一查询/跨表 JOIN）
   - 多格式报告导出（图表 + 文本报告 + 导出）
4. **工作流演示区**（可选）：简化「问→析→图→报告」流程示意。
5. **Footer**。

### 5.2 登录/注册模态弹窗

- 「登录」按钮 → 弹出模态弹窗（`auth.css` + `js/landing.js`），含登录/注册可切换表单。提交成功 → 存 token（按记我开关）→ 跳 `/app`。
- 「免费开始」主按钮 → 同弹窗，默认停「注册」表单。
- 表单字段：账号、密码、确认密码（注册）、记住我复选框。
- 失败：弹窗内显示后端错误信息，不跳转。
- 注册成功：后端已自动登录返回 token，前端直接存 token 跳 `/app`。
- 现有 `LOGIN_PAGE` 表单切换 JS 逻辑迁移到 `js/landing.js`，复用成熟部分。

### 5.3 登录态判断（落地页侧）

`js/auth.js` 加载时调 `GET /api/me` 校验 token：有效 → 导航栏换「进入工作台」+ 用户头像/昵称；无效 → 显示登录/注册入口。

## 6. 主应用重塑（`app.html`）

现有 `HTML_TEMPLATE`（307–~2130 行）**功能与布局保留，视觉指纹重塑**：

| 模块 | 功能（不变） | 视觉（重塑） |
|---|---|---|
| 左侧栏 | 会话列表、新建会话、知识库入口、登出 | 深空/冷底 + 电光蓝强调 + 锐利边框 |
| 主聊天区 | 消息流、welcome 占位、Markdown 渲染 | 科技风气泡/边框、代码块深底高亮 |
| 底部输入框 | 文本输入、发送、SSE 流式 | 克制发光描边、聚焦态高对比 |
| 数据集面板 | 上传/列表/schema/删除 | 冷色表格/卡片 |
| 图表展示 | `[CHART:url]` 嵌入 | 图表配色与 token 协同 |
| 顶部用户区 | 头像/昵称/设置 | 科技风用户胶囊 |

约束：Hallmark redesign 抛弃现有结构指纹但保留 IA 和交互逻辑——侧栏/主区/输入框功能位置不动，只换视觉。现有原生 JS（SSE 解析、`[THINKING]`/`[SESSION]`/`[SESSIONS_RELOAD]`/`[CHART:url]`/`[CONTEXT]`/`[AUDIT]`/`[DONE]`/`[ERROR]` token 处理、会话切换、数据集 CRUD）迁移到 `js/app.js`，逻辑不改。

### 6.1 SSE 协议不变

`POST /api/chat` 事件 token 与心跳机制 `_stream_with_heartbeat` 完全不动。前端解析逻辑迁移但行为不变。

## 7. 迁移策略与风险控制

### 7.1 阶段（每阶段独立可验证、可回滚）

| 阶段 | 内容 | 验证 | 风险 |
|---|---|---|---|
| P0 基建 | 建 `agent/static/` 骨架；新增 `agent/api/auth.py`（`require_auth` + LRU 缓存 + `/api/me`）；不删旧代码 | `pytest tests/` 全绿，旧前端仍可用 | 低 |
| P1 抽静态文件 | `LOGIN_PAGE`→`static/index.html`（暂旧样式），`HTML_TEMPLATE`→`static/app.html`+`css/app.css`+`js/app.js`；StaticFiles 挂 `/static`，`/` `/app` 返回静态文件 | 手测渲染与迁移前一致；SSE/数据集/会话切换全功能不变 | 中 |
| P2 鉴权强化 | 业务 API 挂 `Depends(require_auth)`；`GET /app` 服务端校验重定向；`auth.js` 统一 fetch + 401 跳转；记我 | 未登录访问 `/app`→302`/`；未登录调 `/api/*`→401；登录后全功能正常 | 中 |
| P3 Hallmark 安装+重塑 | `npx skills add nutlope/hallmark`；`hallmark study` hum-07；`hallmark redesign` 按科技风 brief 重做 `index.html`/`app.html`+`tokens.css` | 视觉验收：两页同主题、科技风、非 AI-slop；功能回归 P1 全绿 | 中 |
| P4 落地页登录弹窗 | `auth.css`+`js/landing.js` 实现弹窗、表单切换、记我、错误处理；已登录态导航变「进入工作台」 | 注册→自动登录→跳 `/app`；登录失败弹窗内报错；记我生效 | 低 |
| P5 清理 | 删 `fastapi_server.py` 的 `LOGIN_PAGE`/`HTML_TEMPLATE` 字符串常量；删 `_get_user_id` anonymous 兜底死代码 | 全功能回归 + `pytest` 全绿 | 低 |

### 7.2 P1 拆分 3216 行单文件安全策略

1. 先迁 CSS 后迁 JS：`<style>`→`css/app.css`，`<script>`→`js/app.js`，结构留 `app.html`。三段独立后 diff 易查遗漏。
2. `app.js` 中 `[THINKING]` 等字符串和 SSE 解析逻辑**只挪位置不改逻辑**，逐函数对照。
3. Python 字符串里的 `{` 转义：现有 HTML 是普通字符串非 f-string，迁移到 `.html` 后不再受 Python 字符串约束；确认旧字符串无 f-string 动态插值（已确认无）。
4. 静态资源路径统一用 `/static/` 前缀。
5. 图表运行时 HTML 挂 `/reports` 逻辑不动。

### 7.3 不改动的子系统（隔离风险面）

明确不碰：LLM/RAG/ChromaDB/rerank；DuckDB 多源/SQL 沙箱/`safe_ident`；`user_db` 表结构/bcrypt/token 生成（只加缓存层）；SSE 协议 token/心跳；数据集 CRUD 业务逻辑/知识库 reindex。鉴权改动只在外层包依赖，不深入子系统内部。

### 7.4 回滚保障

- 每阶段一个 commit；P0–P2 保留旧字符串常量不删（P5 才删）。
- P3 Hallmark 重塑不满意可 `git revert` 单 commit 回到 P2（静态文件 + 旧样式 + 强鉴权），鉴权与架构成果保留，只退视觉。
- 分支 `feat/welcome-page-ui` 从 `feat/config-file-report` 拉出，不污染配置改造分支。

### 7.5 测试策略

- 既有 `tests/` 全阶段保持绿。
- 新增：`tests/test_auth.py`（`require_auth` 401/通过、`/api/me`、token 过期、`GET /app` 重定向）；可选 `tests/test_static_serving.py`（静态文件 200、`/static/*`）。
- 前端无自动化测试，靠手测清单：登录/注册/记我/401 跳转/SSE 流/数据集上传/会话切换/图表/报告导出。

## 8. 关键文件清单

- `agent/api/fastapi_server.py` — 路由改造、删字符串常量、StaticFiles 挂载
- `agent/api/auth.py` — **新增**，`require_auth` + LRU 缓存 + `/api/me`
- `agent/static/*` — **新增**，全部前端静态资源
- `agent/database/user_db.py` — 不改存储，仅可能被 `auth.py` 调用
- `~/.claude/skills/hallmark/` — Hallmark 技能安装位置
- `tests/test_auth.py` — **新增**

## 9. 非目标（YAGNI）

- 不引入前端构建工具/打包器（保持纯静态 + 原生 JS）。
- 不引入 Vue/React/Tailwind 等 CDN 框架。
- 不做长时 token / refresh token 机制（24h + 记我足够）。
- 不做 OAuth/第三方登录。
- 不改造后端业务子系统（见 7.3）。
