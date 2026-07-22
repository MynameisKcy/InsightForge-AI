# 欢迎页 + 主应用 UI 重塑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Hallmark 设计技能重塑 InsightForge AI 的前端视觉（科技风），新建欢迎落地页内嵌登录入口，把前端从 Python 字符串抽成静态文件，并实现全站服务端强制鉴权。

**Architecture:** 前端从 `fastapi_server.py` 的 HTML 字符串常量抽出到 `agent/static/` 静态文件，FastAPI 用 `StaticFiles` 挂载。鉴权用统一 `require_auth` 依赖（含 `validate_token` 进程内 LRU 短缓存）挂在所有业务 `/api/*` 路由上，`GET /app` 服务端校验 token 失败重定向 `/`。Hallmark 装到 `~/.claude/skills/hallmark/`，`study` hum-07 + `redesign` 主应用，brief 锁定科技风。

**Tech Stack:** FastAPI、Starlette StaticFiles、SQLite+bcrypt（现有 `user_db`）、原生 JS/CSS（无构建工具）、Hallmark 设计技能。

## Global Constraints

- 所有 Python/pytest 命令须在 `AnalysisAgent` conda 环境内运行：
  `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent`
- 工作目录 `agent/`：`cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent`
- 分支 `feat/welcome-page-ui`（已从 `feat/config-file-report` 拉出，当前 HEAD）。
- 配色科技风：冷色高对比、电光蓝强调、深空或冷白底、几何无衬线、锐利描边、低圆角。排除暖色（Hum 风不适用）。
- 不引入构建工具/打包器/Vue/React/Tailwind CDN。
- 不改后端业务子系统（LLM/RAG/DuckDB/SQL 沙箱/user_db 表结构/SSE 协议）。
- `user_db.validate_token(token)` 现有签名：返回 `{"user_id","account","nickname","avatar_path"} | None`（`user_db.py:196`）。
- `user_db` 导入在 `fastapi_server.py:99-102`（try/except 双路径）。
- `app = FastAPI(...)` 在 `fastapi_server.py:105`。
- 现有 `_get_user_id(request)` 在 `fastapi_server.py:153-160`，匿名返回 `"anonymous"`。

## File Structure

- `agent/api/auth.py` — **新建**。`require_auth` 依赖、`validate_token_cached`（LRU 短缓存）、`get_current_user`、`extract_token`。单一职责：认证与鉴权。
- `agent/static/index.html` — **新建**。欢迎落地页（P1 暂用迁移旧样式，P3 Hallmark 重塑）。
- `agent/static/app.html` — **新建**。主应用 HTML 结构（P1 迁移，P3 重塑）。
- `agent/static/css/tokens.css` — **新建**。全站设计 token（科技风）。
- `agent/static/css/landing.css` — **新建**。落地页样式。
- `agent/static/css/app.css` — **新建**。主应用样式（P1 迁移自 `HTML_TEMPLATE` 内 `<style>`）。
- `agent/static/css/auth.css` — **新建**。登录/注册弹窗样式。
- `agent/static/js/auth.js` — **新建**。token 存取、记我、统一 fetch、401 跳转、`/api/me` 校验。
- `agent/static/js/landing.js` — **新建**。落地页交互（弹窗、表单切换、提交）。
- `agent/static/js/app.js` — **新建**。主应用原生 JS 迁移（SSE/会话/数据集/侧栏）。
- `agent/api/fastapi_server.py` — **修改**。路由改造（`/` `/app` 改静态文件、`/app` 加重定向、`/api/*` 挂 `require_auth`、新增 `/api/me`、StaticFiles 挂载、删 `LOGIN_PAGE`/`HTML_TEMPLATE` 常量）。
- `tests/test_auth.py` — **新建**。鉴权与 `/api/me` 测试。

---

### Task 1: 新建鉴权模块 `agent/api/auth.py`

**Files:**
- Create: `agent/api/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: `user_db.validate_token(token: str) -> dict | None`（返回 `{"user_id","account","nickname","avatar_path"}` 或 `None`）。`user_db` 导入双路径：`from database.user_db import user_db`（失败则 `from agent.database.user_db import user_db`）。
- Produces:
  - `extract_token(request: Request) -> str | None` — 从 `Authorization: Bearer <token>` 提取 token。
  - `validate_token_cached(token: str) -> dict | None` — 包 LRU 短缓存（30s）的 `validate_token`。
  - `require_auth(request: Request) -> dict` — FastAPI 依赖，失败抛 `HTTPException(401)`，成功返回 `{"user_id","account","nickname","avatar_path"}`。
  - `get_current_user(request: Request) -> dict | None` — 非抛错版，供 `GET /app` 重定向判断用。

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth.py`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import database.user_db as user_db_mod


def _register(name: str = "authtest"):
    user_db_mod.register(name, "Test1234!")


def _login(name: str = "authtest"):
    return user_db_mod.user_db.login(name, "Test1234!")


def _make_request(token: str | None):
    class H:
        def __init__(self, t): self._h = {"authorization": f"Bearer {t}"} if t else {}
        def get(self, k, d=""): return self._h.get(k.lower(), d)
    class R:
        def __init__(self, t): self.headers = H(t)
    return R(token)


def test_require_auth_rejects_missing_token():
    from api.auth import require_auth
    import pytest
    with pytest.raises(Exception) as e:
        require_auth(_make_request(None))
    assert "401" in str(e.value) or e.value.status_code == 401


def test_require_auth_accepts_valid_token():
    from api.auth import require_auth, extract_token, validate_token_cached
    _register()
    login_res = _login()
    token = login_res["token"]
    req = _make_request(token)
    user = require_auth(req)
    assert user["user_id"]
    assert user["account"] == "authtest"
    # 缓存命中路径
    assert validate_token_cached(token)["user_id"] == user["user_id"]
    # extract_token
    assert extract_token(req) == token


def test_require_auth_rejects_bad_token():
    from api.auth import require_auth
    import pytest
    with pytest.raises(Exception):
        require_auth(_make_request("not-a-real-token"))


def test_get_current_user_none_when_no_token():
    from api.auth import get_current_user
    assert get_current_user(_make_request(None)) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent && python -m pytest tests/test_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'api.auth'`

- [ ] **Step 3: Write minimal implementation**

Create `agent/api/auth.py`:

```python
"""统一鉴权：require_auth 依赖 + validate_token 进程内短缓存。"""
import time
from fastapi import Request, HTTPException

try:
    from database.user_db import user_db
except ImportError:
    from agent.database.user_db import user_db

# token -> (user_dict, expire_ts)；30s 短缓存，token 24h 有效，安全
_CACHE_TTL = 30.0
_token_cache: dict[str, tuple[dict, float]] = {}


def extract_token(request: Request) -> str | None:
    """从 Authorization: Bearer <token> 提取 token。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def validate_token_cached(token: str | None) -> dict | None:
    """包 LRU 短缓存的 validate_token。命中不查库。"""
    if not token:
        return None
    now = time.time()
    cached = _token_cache.get(token)
    if cached and cached[1] > now:
        return cached[0]
    user = user_db.validate_token(token)
    _token_cache[token] = (user, now + _CACHE_TTL)
    # 简易清理：缓存超 1000 项时丢掉过期项
    if len(_token_cache) > 1000:
        for k in [k for k, v in _token_cache.items() if v[1] <= now]:
            _token_cache.pop(k, None)
    return user


def require_auth(request: Request) -> dict:
    """FastAPI 依赖：业务路由强制鉴权，失败 401。返回用户 dict。"""
    token = extract_token(request)
    user = validate_token_cached(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def get_current_user(request: Request) -> dict | None:
    """非抛错版，供 GET /app 重定向判断用。"""
    return validate_token_cached(extract_token(request))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent && python -m pytest tests/test_auth.py -v`
Expected: PASS（4 tests）

- [ ] **Step 5: Commit**

```bash
git add agent/api/auth.py tests/test_auth.py
git commit -m "feat(auth): require_auth dependency + validate_token LRU cache"
```

---

### Task 2: 挂 `require_auth` 到业务 API + 新增 `/api/me` + `GET /app` 重定向

**Files:**
- Modify: `agent/api/fastapi_server.py`（多处路由，见步骤）
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: Task 1 的 `require_auth`、`get_current_user`。
- Produces: `GET /api/me` 路由（返回 `{user_id, account, nickname, avatar_path}`）；所有业务 `/api/*` 强制鉴权；`GET /app` 未登录 302→`/`。

- [ ] **Step 1: Write the failing test (append to `tests/test_auth.py`)**

```python
def test_api_me_requires_auth():
    from fastapi.testclient import TestClient
    from api.fastapi_server import app
    client = TestClient(app)
    # 未登录 401
    r = client.get("/api/me")
    assert r.status_code == 401
    # 登录后返回用户
    _register("metest")
    tok = _login("metest")["token"]
    r = client.get("/api/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["account"] == "metest"


def test_app_redirects_when_unauthenticated():
    from fastapi.testclient import TestClient
    from api.fastapi_server import app
    client = TestClient(app)
    r = client.get("/app", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent && python -m pytest tests/test_auth.py::test_api_me_requires_auth tests/test_auth.py::test_app_redirects_when_unauthenticated -v`
Expected: FAIL（`/api/me` 404；`/app` 返回 200 非 302）

- [ ] **Step 3a: Add imports and `/api/me` route**

In `agent/api/fastapi_server.py`, after the existing import block (around line 105 after `app = FastAPI(...)`), add:

```python
from starlette.responses import FileResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from api.auth import require_auth, get_current_user
import os as _os

_STATIC_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static")
```

Then add the `/api/me` route near the other auth routes (after `/api/logout` at ~line 2226):

```python
@app.get("/api/me")
async def api_me(user=Depends(require_auth)):
    """返回当前登录用户信息。未登录 401。"""
    return JSONResponse({
        "user_id": user["user_id"],
        "account": user["account"],
        "nickname": user.get("nickname"),
        "avatar_path": user.get("avatar_path"),
    })
```

Also ensure `Depends` is imported — update line 83 to:
```python
from fastapi import FastAPI, Request, Header, UploadFile, File, Depends
```

- [ ] **Step 3b: Replace `GET /` and `GET /app` with static-file serving + redirect**

Replace lines 2229-2238 (the `index` and `app_page` functions) with:

```python
@app.get("/")
async def index():
    """返回欢迎落地页。"""
    return FileResponse(_os.path.join(_STATIC_DIR, "index.html"))


@app.get("/app")
async def app_page(request: Request):
    """主应用：未登录重定向到落地页。"""
    if not get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(_os.path.join(_STATIC_DIR, "app.html"))
```

Mount static files (add after the `index` route, before `/api/chat`):

```python
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
```

> Note: `app.html`/`index.html` do not exist yet (created in Task 3). These two tests will pass once Task 3 creates them; for now `GET /app` returning 302 (redirect, no file read) already passes `test_app_redirects_when_unauthenticated`, and `/api/me` passes regardless. Run the two target tests.

- [ ] **Step 3c: Attach `require_auth` to all business endpoints**

For each of these routes, add `user=Depends(require_auth)` to the signature and replace `user_id = await _get_user_id(request)` with `user_id = user["user_id"]`:

Routes (path: line of `@app` decorator):
- `POST /api/chat` (2241, signature `api_chat(request)` → `api_chat(request, user=Depends(require_auth))`); remove the `if user_id == "anonymous": return 401` block (2251-2252) since `require_auth` covers it.
- `POST /api/analysis` (2353)
- `GET /api/conversation/history` (2389)
- `GET /api/sessions` (2399)
- `GET /api/sessions/{session_id}` (2409)
- `DELETE /api/sessions/{session_id}` (2431)
- (the second `_get_user_id` at 2453 — inspect which route it belongs to; add `require_auth` there too)
- `GET /api/datasets` (2490)
- `POST /api/datasets/upload` (2504)
- `DELETE /api/datasets/{name}` (2620)
- `GET /api/datasets/{name}/schema` (2667)
- `POST /api/datasources/reload` (2725)
- `GET /api/settings/status` (2776)
- `GET /api/settings` (2785)
- `POST /api/settings` (2797)
- `GET /api/profile` (2840)
- `POST /api/profile` (2855)
- `POST /api/avatar` (2872)
- `POST /api/password` (2893)
- `GET /api/knowledge/files` (2911)
- `GET /api/files` (2947)
- `POST /api/knowledge/upload` (3001)
- `DELETE /api/knowledge/files/{filename}` (3049)
- `POST /api/knowledge/reindex` (3072)
- `GET /api/knowledge/stats` (3090)

Leave unchanged: `POST /api/register`, `POST /api/login`, `POST /api/logout`, `GET /api/health`. Do **not** delete `_get_user_id` yet (Task 9 cleanup).

- [ ] **Step 4: Run test to verify it passes**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent && python -m pytest tests/test_auth.py -v`
Expected: PASS（all tests, including `/api/me` 401/200 and `/app` redirect）

- [ ] **Step 5: Smoke-check existing tests still green**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent && python -m pytest tests/ -v`
Expected: PASS (existing tests; some may now need a token in fixtures — if a previously-anonymous test breaks, update that test to register+login and pass a Bearer token; do not loosen `require_auth`)

- [ ] **Step 6: Commit**

```bash
git add agent/api/fastapi_server.py tests/test_auth.py
git commit -m "feat(auth): enforce require_auth on /api/* + GET /app redirect + /api/me"
```

---

### Task 3: 抽静态文件骨架（P1 迁移旧 UI，暂不改视觉）

**Files:**
- Create: `agent/static/index.html`（从 `LOGIN_PAGE` 字符串 165-304 迁移）
- Create: `agent/static/app.html`（从 `HTML_TEMPLATE` 307-~2130 的 HTML 结构部分迁移）
- Create: `agent/static/css/app.css`（从 `HTML_TEMPLATE` 内 `<style>` 迁移）
- Create: `agent/static/js/app.js`（从 `HTML_TEMPLATE` 内 `<script>` 迁移）
- Create: `agent/static/css/tokens.css`（占位空文件，P3 填）
- Create: `agent/static/css/landing.css`、`agent/static/css/auth.css`（占位空文件）

**Interfaces:**
- Produces: 四个静态文件，使 Task 2 的 `FileResponse` 能命中真实文件；前端功能与迁移前一致。

- [ ] **Step 1: Create static dir + placeholder files**

Run:
```bash
cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent
mkdir -p static/css static/js
touch static/css/tokens.css static/css/landing.css static/css/auth.css
```

- [ ] **Step 2: Migrate `LOGIN_PAGE` → `static/index.html`**

Copy the entire string body of `LOGIN_PAGE` (lines 165-304, between the triple-quotes) verbatim into `agent/static/index.html`. Adjust the `<style>` references if any: this page currently has inline `<style>` — keep it inline for now (P3 will extract to `landing.css`/`auth.css`). Update any absolute `window.location.href = '/app'` references — keep as-is (still valid). Verify no f-string `{...}` interpolation existed (confirmed none in spec).

- [ ] **Step 3: Migrate `HTML_TEMPLATE` → `static/app.html` + `app.css` + `app.js`**

Extract the `HTML_TEMPLATE` string (lines 307-~2130):
- HTML structure (everything outside `<style>` and `<script>`) → `agent/static/app.html`. In the `<head>`, replace inline `<style>...</style>` with:
  ```html
  <link rel="stylesheet" href="/static/css/app.css">
  <link rel="stylesheet" href="/static/css/tokens.css">
  ```
- The `<style>` block contents → `agent/static/css/app.css` (verbatim).
- The `<script>` block contents → `agent/static/js/app.js` (verbatim, logic unchanged). In `app.html`, replace inline `<script>...</script>` with `<script src="/static/js/app.js"></script>`.

In `app.js`, the top auth-redirect block (`if (!authToken) { window.location.href = '/'; }` ~line 921-925 of original) stays — it's a harmless client-side guard complementing server redirect.

- [ ] **Step 4: Verify the server serves migrated files and UI renders identically**

Run the server:
```bash
eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent && python -m api.fastapi_server
```
Open `http://localhost:8502/` — expect the login page renders as before.
Manually: register a test account → auto-redirect to `/app` → verify sidebar, chat input, dataset panel, knowledge all function. Open `/app` directly without login → expect redirect to `/`.

- [ ] **Step 5: Commit**

```bash
git add agent/static
git commit -m "feat(static): extract frontend to agent/static/ (P1 migration, no visual change)"
```

---

### Task 4: 新建统一 `auth.js`（token 存取 + 记我 + 401 拦截 + `/api/me`）

**Files:**
- Create: `agent/static/js/auth.js`

**Interfaces:**
- Produces:
  - `window.Auth.getToken()` — 返回 localStorage 或 sessionStorage 中的 token。
  - `window.Auth.setToken(token, remember)` — remember=true 存 localStorage，否则 sessionStorage。
  - `window.Auth.clearToken()` — 清两个存储。
  - `window.Auth.authedFetch(url, opts)` — 带 `Authorization` 头的 fetch；401 自动清 token + 跳 `/`。
  - `window.Auth.fetchMe()` — 调 `/api/me` 返回用户信息或 null。

- [ ] **Step 1: Write `auth.js`**

Create `agent/static/js/auth.js`:

```javascript
window.Auth = (function () {
  function getToken() {
    return localStorage.getItem('token') || sessionStorage.getItem('token') || '';
  }
  function setToken(token, remember) {
    clearToken();
    if (remember) localStorage.setItem('token', token);
    else sessionStorage.setItem('token', token);
  }
  function clearToken() {
    localStorage.removeItem('token');
    sessionStorage.removeItem('token');
  }
  async function authedFetch(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers || {}, {
      'Authorization': 'Bearer ' + getToken(),
    });
    const res = await fetch(url, opts);
    if (res.status === 401) {
      clearToken();
      window.location.href = '/';
      return res;
    }
    return res;
  }
  async function fetchMe() {
    try {
      const res = await authedFetch('/api/me');
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }
  return { getToken, setToken, clearToken, authedFetch, fetchMe };
})();
```

- [ ] **Step 2: Wire `app.html` to use `auth.js`**

In `agent/static/app.html` `<head>`, add before `app.js`:
```html
<script src="/static/js/auth.js"></script>
```
In `agent/static/js/app.js`, replace the token-reading lines:
```javascript
let authToken = sessionStorage.getItem('token') || '';
let accountName = sessionStorage.getItem('account') || '';
```
with:
```javascript
let authToken = Auth.getToken();
let accountName = '';
```
Replace all `fetch('/api/...')` calls in `app.js` with `Auth.authedFetch('/api/...', {...})` so 401 is uniformly handled. (Search for `fetch(` occurrences in `app.js` and wrap each.)

- [ ] **Step 3: Verify**

Run server, log in, use the app (send a chat message, open datasets). Confirm no console errors and 401 path works: manually clear token in DevTools (`sessionStorage.clear()`) then send a message → expect redirect to `/`.

- [ ] **Step 4: Commit**

```bash
git add agent/static/js/auth.js agent/static/app.html agent/static/js/app.js
git commit -m "feat(auth): unified auth.js (token store + remember-me + 401 redirect)"
```

---

### Task 5: 落地页登录/注册模态弹窗（`landing.js` + `auth.css`，暂旧样式）

**Files:**
- Modify: `agent/static/index.html`（加导航条「登录」/「免费开始」按钮、弹窗容器、引入 auth.js/landing.js/auth.css）
- Create: `agent/static/js/landing.js`
- Create: `agent/static/css/auth.css`（弹窗样式，先复用旧登录卡配色，P3 重塑）

**Interfaces:**
- Consumes: Task 4 的 `Auth.setToken/clearToken/fetchMe`.
- Produces: 落地页登录/注册弹窗，提交调 `/api/login`/`/api/register`，成功存 token 跳 `/app`；已登录态导航显示「进入工作台」。

- [ ] **Step 1: Add modal + nav to `index.html`**

In `<head>` add:
```html
<link rel="stylesheet" href="/static/css/auth.css">
<script src="/static/js/auth.js"></script>
<script src="/static/js/landing.js" defer></script>
```
Add a top nav bar with buttons `id="btn-login"` (text「登录」) and `id="btn-start"` (text「免费开始」), plus an authed-state link `id="btn-workbench"` (text「进入工作台 →」) hidden by default. Add a modal container `id="auth-modal"` with a form containing: hidden input `data-mode` (login|register), `#auth-account`, `#auth-password`, `#auth-password2` (register only, hidden in login mode), `#auth-remember` checkbox, submit button, error display `#auth-error`, and a toggle link `#auth-toggle` to switch login/register.

- [ ] **Step 2: Write `landing.js`**

Create `agent/static/js/landing.js` implementing:
- `btn-login` click → open modal in login mode.
- `btn-start` click → open modal in register mode.
- `auth-toggle` click → switch mode (toggle `#auth-password2` visibility + button text).
- form submit → POST `/api/login` or `/api/register` (use `Auth.authedFetch` not needed here since these are public; use plain `fetch`):
  ```javascript
  const mode = form.dataset.mode;
  const url = mode === 'login' ? '/api/login' : '/api/register';
  const body = { account: account.value, password: password.value };
  if (mode === 'register') body.password2 = password2.value;
  const res = await fetch(url, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
  const data = await res.json();
  if (data.success && data.token) {
    Auth.setToken(data.token, remember.checked);
    window.location.href = '/app';
  } else {
    authError.textContent = data.error || '操作失败，请重试';
  }
  ```
- On page load: call `Auth.fetchMe()`; if it returns a user, hide `btn-login`/`btn-start` and show `btn-workbench` (and set its text to `进入工作台 →`).

- [ ] **Step 3: Write `auth.css` (basic modal styling, reuse old login palette)**

Style `#auth-modal` as a centered overlay (semi-transparent backdrop + white card, reusing the old `#1a1a2e→#0f3460` gradient backdrop and `#fff` card for now; P3 will restyle). Include `.hidden { display: none; }`.

- [ ] **Step 4: Verify**

Run server, open `/`. Click「登录」→ modal opens in login mode. Switch to register → password2 appears. Register a new account → auto-redirect to `/app`. Log out → back to `/`, now logged-out shows login/start buttons. Re-open `/` while token valid → shows「进入工作台」.

- [ ] **Step 5: Commit**

```bash
git add agent/static/index.html agent/static/js/landing.js agent/static/css/auth.css
git commit -m "feat(landing): login/register modal + remember-me on welcome page"
```

---

### Task 6: 落地页 Hero + 功能特性区内容（介绍项目功能）

**Files:**
- Modify: `agent/static/index.html`

**Interfaces:**
- Produces: 落地页 Hero + 3-4 功能特性卡片 + Footer（内容就位，P3 Hallmark 重塑视觉）。

- [ ] **Step 1: Add Hero and feature sections**

In `index.html`, add (between nav and footer):
- **Hero**: h1「InsightForge AI」+ subtitle「多智能体协同数据分析平台 — 自然语言驱动 SQL 查询、趋势/产品/风险分析、图表生成与多格式报告」+ two CTA buttons「立即开始」(opens register modal) + 「查看示例」(scrolls to features).
- **Features** (grid of 4 cards):
  1. **自然语言驱动** — Smart Assistant 智能客服模式，ReAct Agent 配 13 个工具，对话式流式响应。
  2. **多智能体分析流水线** — PlannerAgent 编排：SQL → 趋势/产品/风险分析 → 可视化 → 报告 → 导出，步骤依赖自动调度。
  3. **多源数据管理** — 上传 CSV/Excel 或预配置 MySQL/PostgreSQL，DuckDB 统一查询，支持跨表 JOIN。
  4. **多格式报告导出** — 自动生成图表与文本报告，一键导出。
- **Footer**: 简短版权/说明.

- [ ] **Step 2: Verify content renders**

Run server, open `/`, scroll through Hero + features. Confirm copy is accurate to CLAUDE.md project description.

- [ ] **Step 3: Commit**

```bash
git add agent/static/index.html
git commit -m "feat(landing): hero + feature cards introducing project capabilities"
```

---

### Task 7: 安装 Hallmark 技能到 `~/.claude/skills/hallmark/`

**Files:**
- Install: `~/.claude/skills/hallmark/`

**Interfaces:**
- Produces: Hallmark 设计技能可用（`hallmark study`、`hallmark redesign` 动词）。

- [ ] **Step 1: Install via npx**

Run:
```bash
npx skills add nutlope/hallmark
```
If `npx`/network unavailable, fallback: clone/copy `SKILL.md` + `references/` from `https://github.com/Nutlope/hallmark/tree/main/skills/hallmark` into `C:/Users/86131/.claude/skills/hallmark/`.

- [ ] **Step 2: Verify skill present**

Run:
```bash
ls C:/Users/86131/.claude/skills/hallmark/
```
Expected: `SKILL.md` and `references/` present.

- [ ] **Step 3: Commit (record install in repo notes)**

No source change; commit a note:
```bash
echo "Hallmark design skill installed to ~/.claude/skills/hallmark/ (npx skills add nutlope/hallmark)" > docs/HALLMARK_INSTALLED.md
git add docs/HALLMARK_INSTALLED.md
git commit -m "docs: record Hallmark skill installation"
```

---

### Task 8: Hallmark `study` hum-07 提取设计 DNA

**Files:**
- Create: `agent/static/hallmark-study/design.md`（Hallmark `study` 产物）

**Interfaces:**
- Consumes: Task 7 的 Hallmark 技能。
- Produces: `design.md` 描述 hum-07 宏观结构/字体配对/色彩锚点（仅结构参考，配色按科技风 brief 重选）。

- [ ] **Step 1: Run `hallmark study` on hum-07**

Invoke the Hallmark skill: `hallmark study https://www.usehallmark.com/examples/hum-07/` — extract macrostructure, type-pairing, color anchor into a portable `design.md`. Direct it to output at `agent/static/hallmark-study/design.md`.

- [ ] **Step 2: Review the extracted DNA**

Read `design.md`. Confirm it describes hum-07's landing-page macrostructure (Hero + features + auth entry layout). Note the warm palette will NOT be used (科技风 brief overrides in Task 9).

- [ ] **Step 3: Commit**

```bash
git add agent/static/hallmark-study/design.md
git commit -m "feat(hallmark): study hum-07 design DNA (structural reference only)"
```

---

### Task 9: Hallmark `redesign` 落地页 `index.html`（科技风）

**Files:**
- Modify: `agent/static/index.html`
- Modify: `agent/static/css/tokens.css`
- Modify: `agent/static/css/landing.css`
- Modify: `agent/static/css/auth.css`

**Interfaces:**
- Consumes: Task 6 的内容（Hero/features copy + IA），Task 8 的 hum-07 结构 DNA。
- Produces: 科技风重塑的落地页（深空/冷底、电光蓝强调、几何无衬线、锐利描边、低圆角），`tokens.css` 填实。

- [ ] **Step 1: Run `hallmark redesign` on `index.html` with the sci-tech brief**

Invoke Hallmark: `hallmark redesign agent/static/index.html` with brief:
> 多智能体协同数据分析平台（InsightForge AI）：LangChain+LangGraph 编排，自然语言驱动 SQL/趋势/产品/风险分析、图表生成、多格式报告导出；用户是数据分析师；视觉须严肃、可信、信息密集但克制；**配色科技风——冷色高对比、电光蓝强调（如 #3b82f6/#06b6d4 类）、深空底（近黑/深蓝灰，如 #0a0e1a/#0f1729）或冷白底、几何无衬线（Inter/Geist 类）、锐利描边、低圆角**。宏观结构参考 hum-07（Hero + 功能特性 + 登录入口），但配色/字体/质感按科技风，排除温暖手作风。

Keep copy + IA + the auth modal hooks (`btn-login`/`btn-start`/`auth-modal` ids) intact — Hallmark rebuilds structure/visual, preserves interactive anchors.

- [ ] **Step 2: Fill `tokens.css` with the Hallmark theme tokens**

Write the science-tech design tokens Hallmark selected into `agent/static/css/tokens.css` (CSS custom properties on `:root`): background, accent (electric blue), neutral (cool slate/zinc grays), font stack (geometric sans + monospace), shadows (sharp), radii (low). Replace the old `--accent: #e94560` etc.

- [ ] **Step 3: Verify landing renders in sci-tech style**

Run server, open `/`. Confirm: cool dark/space or cool-white background, electric-blue accents, geometric sans font, sharp low-radius cards, modal opens and login/register still works (Task 5 logic intact). Confirm it does NOT look AI-slop (Hallmark's 57 slop-gates passed).

- [ ] **Step 4: Commit**

```bash
git add agent/static/index.html agent/static/css/tokens.css agent/static/css/landing.css agent/static/css/auth.css
git commit -m "feat(ui): Hallmark redesign welcome page in sci-tech theme"
```

---

### Task 10: Hallmark `redesign` 主应用 `app.html`（科技风，保留 IA）

**Files:**
- Modify: `agent/static/app.html`
- Modify: `agent/static/css/app.css`

**Interfaces:**
- Consumes: Task 3 的 `app.js`（IA + 交互逻辑不变），Task 9 的 `tokens.css`（同主题）。
- Produces: 科技风重塑的主应用（侧栏/聊天区/输入框/数据集面板换视觉指纹，功能位置不动）。

- [ ] **Step 1: Run `hallmark redesign` on `app.html`**

Invoke Hallmark: `hallmark redesign agent/static/app.html` with the same sci-tech brief as Task 9. Constraint: **preserve IA and interaction anchors** — the sidebar (session list, new session, knowledge, logout), main chat area (`.welcome-msg`, message stream), bottom input, dataset panel (`ds-section`), chart embed (`[CHART:url]`), top user area (avatar/nickname/settings). Hallmark rebuilds visual fingerprint (colors/borders/typography/texture) on the same DOM hooks. Apply the same `tokens.css` theme so both pages feel like one site.

- [ ] **Step 2: Sync `app.css` to Hallmark output**

Replace `app.css` with Hallmark's redesigned styles for the main app, using the shared `tokens.css` variables (not hardcoded hex, except where Hallmark requires).

- [ ] **Step 3: Verify full functional regression + visual**

Run server, log in, and exercise the manual checklist:
- Sidebar: new session, switch sessions, knowledge entry, logout.
- Chat: send a query → SSE streams (`[THINKING]`/`[DONE]`), chart renders (`[CHART:url]`).
- Datasets: upload CSV, list, view schema, delete.
- Profile: avatar upload, nickname change, password change.
- 401 path: clear token → action → redirect to `/`.
Confirm sci-tech visual consistency with landing page.

- [ ] **Step 4: Commit**

```bash
git add agent/static/app.html agent/static/css/app.css
git commit -m "feat(ui): Hallmark redesign main app in sci-tech theme (IA preserved)"
```

---

### Task 11: 清理（删旧字符串常量 + 死代码）

**Files:**
- Modify: `agent/api/fastapi_server.py`

**Interfaces:**
- Consumes: Tasks 3-10 已完成，前端全部走静态文件。
- Produces: `fastapi_server.py` 不再含 `LOGIN_PAGE`/`HTML_TEMPLATE` 巨字符串；`_get_user_id` 若无残留引用则删。

- [ ] **Step 1: Remove `LOGIN_PAGE` and `HTML_TEMPLATE` constants**

Delete the `LOGIN_PAGE = """..."""` (165-304) and `HTML_TEMPLATE = """..."""` (307-~2130) string constants from `fastapi_server.py`. They are no longer referenced (Task 2 replaced their routes with `FileResponse`).

- [ ] **Step 2: Remove `_get_user_id` if unused**

Search for remaining `_get_user_id` references:
```bash
grep -n "_get_user_id" agent/api/fastapi_server.py
```
If no references remain (all routes now use `Depends(require_auth)`), delete the `_get_user_id` function (153-160). If references remain on a route missed in Task 2, convert that route to `require_auth` first, then delete.

- [ ] **Step 3: Run full test suite + smoke**

Run:
```bash
eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent && python -m pytest tests/ -v
```
Expected: PASS. Then run server and repeat the Task 10 Step 3 manual checklist.

- [ ] **Step 4: Commit**

```bash
git add agent/api/fastapi_server.py
git commit -m "refactor: remove inlined HTML constants + dead _get_user_id (frontend now static)"
```

---

### Task 12: 全量回归 + 收尾

**Files:**
- Verify all.

- [ ] **Step 1: Full test suite**

Run: `eval "$('C:/ProgramData/anaconda3/Scripts/conda.exe' shell.bash hook)" && conda activate AnalysisAgent && cd C:/Users/86131/Multi-Agent-Data-Analysis-System/agent && python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 2: End-to-end manual run**

Run server, complete the full journey:
1. Open `/` → see sci-tech landing with Hero + features.
2. Click「免费开始」→ register modal → register → redirect to `/app`.
3. In `/app`: upload a dataset, send a natural-language query, see SSE stream + chart, export a report.
4. Log out → back to `/`.
5. Log in with「记住我」→ close browser → reopen `/` →「进入工作台」shown (token persisted).
6. Open `/app` in incognito (no token) → redirect to `/`.

- [ ] **Step 3: Update CLAUDE.md if frontend serving changed**

If `fastapi_server.py` structure materially changed (static serving, `/app` redirect), add a 1-2 line note to CLAUDE.md's FastAPI section. Commit if changed.

- [ ] **Step 4: Final commit (if any doc changes)**

```bash
git add CLAUDE.md
git commit -m "docs: note static frontend serving + /app auth redirect"
```

---

## Self-Review

**1. Spec coverage:**
- 欢迎 + 主应用全面重塑 → Tasks 9, 10 ✓
- Hallmark 安装 `~/.claude` → Task 7 ✓
- `study` hum-07 + `redesign` → Tasks 8, 9, 10 ✓
- 科技风配色 → Tasks 9, 10 brief + `tokens.css` ✓
- 落地页内嵌登录（弹窗）→ Tasks 5, 6 ✓
- 全站服务端强制鉴权 → Task 2 ✓
- `GET /app` 重定向 → Task 2 ✓
- 记我开关 → Tasks 4, 5 ✓
- 新增 `/api/me` → Task 2 ✓
- `require_auth` 统一依赖 + LRU 缓存 → Task 1 ✓
- `auth.js` 统一 fetch + 401 → Task 4 ✓
- 抽静态文件 + StaticFiles → Tasks 2, 3 ✓
- 清理旧字符串常量 → Task 11 ✓
- 不碰后端子系统 → 全程约束 ✓

**2. Placeholder scan:** No TBD/TODO. `tokens.css` values intentionally deferred to Hallmark output (Task 9 Step 2) — that's a real instruction, not a placeholder. The 2453 route note ("inspect which route it belongs to") is a verification instruction, acceptable.

**3. Type consistency:** `require_auth` returns `{"user_id","account","nickname","avatar_path"}` (Task 1) — Task 2 reads `user["user_id"]` and `/api/me` reads `user["user_id"]/["account"]/["nickname"]/["avatar_path"]`, matches `user_db.validate_token` return (verified `user_db.py:222-223`). `Auth.setToken/getToken/authedFetch/fetchMe` (Task 4) consumed by Task 5 — names match. `Depends` import added in Task 2 Step 3a before use in 3c.

No gaps found. Plan complete.
