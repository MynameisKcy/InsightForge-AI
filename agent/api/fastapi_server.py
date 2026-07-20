"""
FastAPI Server: 为 AI Data Analyst Multi-Agent System 提供 Web API 和页面。
运行方式: uvicorn api.fastapi_server:app --host 0.0.0.0 --port 8502
"""

import asyncio
import json
import os
import re as re_module
import sys
import traceback
import uuid
from datetime import datetime
from typing import AsyncGenerator

# ── 方案C：加载 .env（DASHSCOPE_API_KEY），须早于任何会实例化模型的导入 ──
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(_env_path, override=False)
except ImportError:
    pass


def _split_sentences(text: str) -> list[str]:
    """将文本按句子分割，保持分隔符在句尾。支持中英文标点。"""
    parts = re_module.split(r'(?<=[。！？.!?\n])\s*', text)
    return [p for p in parts if p.strip()]

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from fastapi import FastAPI, Request, Header, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# ── 记忆系统 & 用户认证 & 数据解析 ──
try:
    from memory.short_term import get_session, ConversationMemory
    from memory.long_term import LongTermMemory
except ModuleNotFoundError:
    from agent.memory.short_term import get_session, ConversationMemory
    from agent.memory.long_term import LongTermMemory

try:
    from database.user_db import user_db
    from database.data_resolver import DataResolver
except ModuleNotFoundError:
    from agent.database.user_db import user_db
    from agent.database.data_resolver import DataResolver

app = FastAPI(title="AI Data Analyst", version="1.0.0")
_long_term_memory = LongTermMemory()

# ── 延迟初始化 Agent ──
_react_agent = None
_planner_agent = None


def _get_react_agent():
    global _react_agent
    if _react_agent is None:
        try:
            from agent.react_agent import ReactAgent
        except ModuleNotFoundError:
            from react_agent import ReactAgent
        _react_agent = ReactAgent()
    return _react_agent


def _get_planner_agent():
    global _planner_agent
    if _planner_agent is None:
        try:
            from agents.planner_agent import PlannerAgent
        except ModuleNotFoundError:
            from agent.agents.planner_agent import PlannerAgent
        _planner_agent = PlannerAgent()
    return _planner_agent


# ── 知识库服务（单例） ──
_vector_store_service = None


def _get_vector_store():
    """延迟初始化向量库服务（方案C：运行时知识库管理）。"""
    global _vector_store_service
    if _vector_store_service is None:
        try:
            from rag.vector_store import VectorStoreService
        except ModuleNotFoundError:
            from agent.rag.vector_store import VectorStoreService
        _vector_store_service = VectorStoreService()
    return _vector_store_service


# ── 用户认证辅助 ──

async def _get_user_id(request: Request) -> str:
    """从请求头中提取用户 token 并验证，返回 user_id 或 'anonymous'。"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        user = user_db.validate_token(token)
        if user:
            return user["user_id"]
    return "anonymous"


# ── HTML 模板 ──

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Data Analyst - 登录</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
       height: 100vh; display: flex; align-items: center; justify-content: center; }
.login-card { background: #fff; border-radius: 16px; padding: 40px; width: 380px;
              box-shadow: 0 20px 60px rgba(0,0,0,.3); }
.login-card h1 { font-size: 22px; color: #1a1a2e; margin-bottom: 4px; }
.login-card .sub { color: #718096; font-size: 13px; margin-bottom: 24px; }
.form-group { margin-bottom: 16px; }
.form-group label { display: block; font-size: 13px; font-weight: 600; color: #4a5568;
                    margin-bottom: 6px; }
.form-group input { width: 100%; padding: 10px 14px; border: 2px solid #e2e8f0;
                    border-radius: 8px; font-size: 14px; outline: none; transition: border-color .2s; }
.form-group input:focus { border-color: #e94560; }
.btn { width: 100%; padding: 12px; border: none; border-radius: 8px; font-size: 15px;
       font-weight: 600; cursor: pointer; transition: background .2s; margin-top: 8px; }
.btn-primary { background: #e94560; color: #fff; }
.btn-primary:hover { background: #c23152; }
.btn-ghost { background: transparent; color: #e94560; font-size: 13px; margin-top: 4px; }
.error-msg { background: #fed7d7; color: #c53030; padding: 10px; border-radius: 8px;
             font-size: 13px; margin-bottom: 12px; display: none; }
.success-msg { background: #c6f6d5; color: #2f855a; padding: 10px; border-radius: 8px;
               font-size: 13px; margin-bottom: 12px; display: none; }
</style>
</head>
<body>
<div class="login-card">
  <h1>🤖 AI Data Analyst</h1>
  <p class="sub">登录以使用数据分析服务</p>
  <div class="error-msg" id="errMsg"></div>
  <div class="success-msg" id="sucMsg"></div>
  <form id="loginForm" onsubmit="handleLogin(event)">
    <div class="form-group">
      <label>账号</label>
      <input type="text" id="account" placeholder="请输入账号" required autofocus>
    </div>
    <div class="form-group">
      <label>密码</label>
      <input type="password" id="password" placeholder="请输入密码" required>
    </div>
    <button type="submit" class="btn btn-primary" id="submitBtn">登 录</button>
  </form>
  <form id="regForm" style="display:none" onsubmit="handleRegister(event)">
    <div class="form-group">
      <label>账号</label>
      <input type="text" id="regAccount" placeholder="请输入账号（至少2位）" required>
    </div>
    <div class="form-group">
      <label>密码</label>
      <input type="password" id="regPassword" placeholder="请输入密码（至少3位）" required>
    </div>
    <div class="form-group">
      <label>确认密码</label>
      <input type="password" id="regPassword2" placeholder="请再次输入密码" required>
    </div>
    <button type="submit" class="btn btn-primary" id="regSubmitBtn">注 册</button>
  </form>
  <button class="btn btn-ghost" id="toggleBtn" onclick="toggleMode()">没有账号？点击注册</button>
</div>
<script>
let isLogin = true;
function toggleMode() {
  isLogin = !isLogin;
  document.getElementById('loginForm').style.display = isLogin ? 'block' : 'none';
  document.getElementById('regForm').style.display = isLogin ? 'none' : 'block';
  document.getElementById('toggleBtn').textContent = isLogin ? '没有账号？点击注册' : '已有账号？点击登录';
  document.getElementById('submitBtn').textContent = isLogin ? '登 录' : '注 册';
  hideMsgs();
}
function hideMsgs() {
  document.getElementById('errMsg').style.display = 'none';
  document.getElementById('sucMsg').style.display = 'none';
}
async function handleLogin(e) {
  e.preventDefault(); hideMsgs();
  const account = document.getElementById('account').value.trim();
  const password = document.getElementById('password').value;
  if (!account || !password) return showErr('请输入账号和密码');
  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = '登录中...';
  try {
    const r = await fetch('/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({account, password})
    });
    const d = await _safeJson(r);
    if (d.success) {
      sessionStorage.setItem('token', d.token);
      sessionStorage.setItem('account', d.account);
      window.location.href = '/app';
    } else showErr(d.error || '登录失败');
  } catch(e) { showErr('网络错误: ' + e.message); }
  finally { btn.disabled = false; btn.textContent = '登 录'; }
}
async function handleRegister(e) {
  e.preventDefault(); hideMsgs();
  const account = document.getElementById('regAccount').value.trim();
  const password = document.getElementById('regPassword').value;
  const password2 = document.getElementById('regPassword2').value;
  if (!account || !password) return showErr('请输入账号和密码');
  if (password !== password2) return showErr('两次密码输入不一致');
  const btn = document.getElementById('regSubmitBtn');
  btn.disabled = true; btn.textContent = '注册中...';
  try {
    const r = await fetch('/api/register', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({account, password})
    });
    const d = await _safeJson(r);
    if (d.success) {
      document.getElementById('sucMsg').textContent = '注册成功！已自动登录...';
      document.getElementById('sucMsg').style.display = 'block';
      sessionStorage.setItem('token', d.token);
      sessionStorage.setItem('account', d.account);
      setTimeout(() => { window.location.href = '/app'; }, 800);
    } else showErr(d.error || '注册失败');
  } catch(e) { showErr('网络错误: ' + e.message); }
  finally { btn.disabled = false; btn.textContent = '注 册'; }
}
async function _safeJson(r) {
  if (!r.ok) {
    try { const e = await r.json(); return e; }
    catch(_) { throw new Error('服务器错误 (HTTP ' + r.status + ')'); }
  }
  return await r.json();
}
function showErr(msg) {
  const el = document.getElementById('errMsg');
  el.textContent = msg; el.style.display = 'block';
}
</script>
</body>
</html>"""


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Data Analyst</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  /* sidebar (dark) */
  --sb-bg: #1a1a2e;
  --sb-bg-elev: #16213e;
  --sb-border: #2d3748;
  --sb-text: #e2e8f0;
  --sb-text-2: #a0aec0;
  --sb-text-3: #8a94a6;
  --sb-text-muted: #718096;
  --sb-icon-bg: #0f3460;
  /* main (light) */
  --main-bg: #f5f7fa;
  --surface: #ffffff;
  --surface-2: #edf2f7;
  --border: #e2e8f0;
  --text: #2d3748;
  --text-2: #4a5568;
  --text-muted: #718096;
  /* brand */
  --accent: #e94560;
  --accent-hover: #c23152;
  --accent-soft: rgba(233,69,96,.12);
  /* semantic */
  --success: #2f855a;
  --danger: #c53030;
  --code-bg: #1a202c;
  --code-fg: #e2e8f0;
  /* depth */
  --shadow-sm: 0 1px 2px rgba(0,0,0,.06), 0 1px 3px rgba(0,0,0,.08);
  --shadow-md: 0 4px 12px rgba(0,0,0,.08), 0 2px 4px rgba(0,0,0,.04);
  --shadow-lg: 0 12px 32px rgba(0,0,0,.12), 0 4px 8px rgba(0,0,0,.06);
  --shadow-input: 0 2px 8px rgba(0,0,0,.06);
  /* radii */
  --r-sm: 6px; --r-md: 8px; --r-lg: 12px; --r-xl: 14px; --r-pill: 999px;
  /* spacing */
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px; --sp-5: 20px; --sp-6: 24px;
  /* type */
  --font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'PingFang SC', 'Microsoft YaHei', sans-serif;
  --font-mono: 'SF Mono', 'Cascadia Code', Consolas, 'Roboto Mono', Menlo, monospace;
  /* motion */
  --t-fast: .15s ease; --t-med: .25s ease; --t-slow: .4s cubic-bezier(.4,0,.2,1);
}
body { font-family: var(--font-sans);
       background: var(--main-bg); height: 100vh; display: flex; overflow: hidden; }

/* ── 侧边栏 ── */
.sidebar { width: 280px; min-width: 280px; height: 100vh; background: var(--sb-bg);
           display: flex; flex-direction: column; border-right: 1px solid var(--sb-border); }
.sidebar-header { padding: var(--sp-4); border-bottom: 1px solid var(--sb-border); }
.sidebar-header h1 { font-size: 16px; color: var(--surface); margin-bottom: var(--sp-3); }
.sidebar-header .user-info { font-size: 12px; color: var(--sb-text-2); margin-bottom: var(--sp-2); }
.btn-new-session { width: 100%; padding: 10px; background: var(--accent); color: var(--surface);
                    border: none; border-radius: var(--r-md); font-size: 14px; cursor: pointer;
                    transition: background var(--t-fast); }
.btn-new-session:hover { background: var(--accent-hover); }
.session-list { flex: none; overflow-y: auto; padding: var(--sp-2) 0; }
.session-item { padding: var(--sp-3) var(--sp-4); cursor: pointer; transition: background var(--t-fast);
                border-left: 3px solid transparent; }
.session-item:hover { background: var(--sb-bg-elev); }
.session-item.active { background: var(--sb-bg-elev); border-left-color: var(--accent); }
.session-item .s-title { color: var(--sb-text); font-size: 13px; font-weight: 500;
                         white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                         margin-bottom: var(--sp-1); }
.session-item .s-time { color: var(--sb-text-3); font-size: 11px; }
.sidebar-footer { padding: var(--sp-3) var(--sp-4); border-top: 1px solid var(--sb-border); }
.btn-logout { width: 100%; padding: var(--sp-2); background: transparent; color: var(--sb-text-2);
              border: 1px solid var(--text-2); border-radius: var(--r-sm); font-size: 13px;
              cursor: pointer; transition: all var(--t-fast); }
.btn-logout:hover { color: var(--accent); border-color: var(--accent); }
.no-sessions { padding: var(--sp-5) var(--sp-4); color: var(--sb-text-3); font-size: 13px; text-align: center; }

/* ── 主内容区 ── */
.main-area { flex: 1; display: flex; flex-direction: column; height: 100vh; position: relative; }
.scroll-bottom-btn {
  position: absolute; right: var(--sp-6); bottom: 90px;
  width: 40px; height: 40px; border-radius: var(--r-pill);
  background: var(--surface); color: var(--accent);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-md);
  font-size: 18px; cursor: pointer;
  opacity: 0; visibility: hidden;
  transform: translateY(8px);
  transition: opacity var(--t-med), transform var(--t-med), visibility var(--t-med);
  z-index: 10;
}
.scroll-bottom-btn.show { opacity: 1; visibility: visible; transform: translateY(0); }
.scroll-bottom-btn:hover { background: var(--accent); color: #fff; }
.chat-container { flex: 1; overflow-y: auto; padding: var(--sp-5) var(--sp-6); display: flex;
                  flex-direction: column; gap: var(--sp-4); max-width: 900px;
                  margin: 0 auto; width: 100%; position: relative; }
.message { display: flex; gap: var(--sp-3); max-width: 85%; animation: fadeIn .3s; }
.message.user { align-self: flex-end; flex-direction: row-reverse; }
.message.assistant { align-self: flex-start; }
.avatar { width: 36px; height: 36px; border-radius: 50%; display: flex;
          align-items: center; justify-content: center; font-size: 18px;
          flex-shrink: 0; box-shadow: var(--shadow-sm); border: 2px solid var(--surface); }
.message.user .avatar { background: var(--accent); }
.message.assistant .avatar { background: var(--sb-icon-bg); }
.bubble { border-radius: var(--r-xl); padding: var(--sp-3) var(--sp-4); line-height: 1.6; font-size: 14px;
          word-break: break-word; }
.message.user .bubble { background: var(--accent); color: var(--surface);
                         border-bottom-right-radius: var(--r-sm);
                         box-shadow: var(--shadow-sm); }
.message.assistant .bubble { background: var(--surface); color: var(--text);
                              border: 1px solid var(--border);
                              border-bottom-left-radius: var(--r-sm);
                              box-shadow: var(--shadow-md); }
.bubble h1,.bubble h2,.bubble h3 { margin: var(--sp-2) 0 var(--sp-1); font-size: 15px; }
.bubble table { border-collapse: collapse; width: 100%; margin: var(--sp-2) 0; font-size: 12px;
                border-radius: var(--r-md); overflow: hidden; box-shadow: var(--shadow-sm); }
.bubble th,.bubble td { border: 1px solid var(--border); padding: var(--sp-2) var(--sp-3); text-align: left; }
.bubble th { background: var(--surface-2); font-weight: 600; color: var(--text-2); }
.bubble tbody tr:nth-child(even) { background: var(--surface-2); }
.bubble tbody tr:hover { background: var(--accent-soft); }
.bubble ul,.bubble ol { padding-left: 20px; margin: var(--sp-1) 0; }
.bubble code { background: var(--surface-2); padding: 1px 4px; border-radius: 3px; font-size: 12px; font-family: var(--font-mono); }
.bubble pre { background: var(--code-bg); color: var(--code-fg); padding: var(--sp-3) var(--sp-4);
              border-radius: var(--r-md); overflow-x: auto; font-family: var(--font-mono);
              font-size: 12.5px; line-height: 1.5; margin: var(--sp-2) 0; position: relative; }
.bubble pre code { background: none; padding: 0; font-size: inherit; color: inherit; font-family: inherit; }
.bubble blockquote { border-left: 3px solid var(--accent); padding-left: var(--sp-3);
                     color: var(--text-muted); margin: var(--sp-2) 0; }
.bubble hr { border: none; border-top: 1px solid var(--border); margin: var(--sp-3) 0; }
.bubble img { max-width: 100%; border-radius: var(--r-md); }
.msg-meta {
  display: flex; align-items: center; gap: var(--sp-1);
  font-size: 10px; color: var(--text-muted);
  margin-top: var(--sp-1);
  font-variant-numeric: tabular-nums;
}
.message.user .msg-meta { justify-content: flex-end; }
.input-area { padding: var(--sp-4) var(--sp-6); background: var(--surface);
              border-top: 1px solid var(--border);
              box-shadow: 0 -2px 12px rgba(0,0,0,.04); }
.input-row { display: flex; gap: var(--sp-3); max-width: 900px; margin: 0 auto; }
.input-row input { flex: 1; padding: var(--sp-3) var(--sp-4); border: 2px solid var(--border);
                    border-radius: var(--r-lg); font-size: 14px; outline: none;
                    box-shadow: var(--shadow-input);
                    transition: border-color var(--t-fast), box-shadow var(--t-fast); }
.input-row input:focus { border-color: var(--accent);
                          box-shadow: 0 0 0 3px var(--accent-soft); }
.input-row button { padding: var(--sp-3) var(--sp-6); background: var(--accent); color: var(--surface);
                    border: none; border-radius: var(--r-lg); font-size: 14px; font-weight: 600;
                    cursor: pointer; transition: background var(--t-fast); }
.input-row button:hover { background: var(--accent-hover); }
.input-row button:disabled { opacity: .6; cursor: not-allowed; }
.typing-indicator { display: flex; gap: var(--sp-1); padding: var(--sp-2) 0; }
.typing-indicator span { width: 8px; height: 8px; background: var(--sb-text-2); border-radius: 50%;
                         animation: bounce 1.2s infinite; }
.typing-indicator span:nth-child(2) { animation-delay: .2s; }
.typing-indicator span:nth-child(3) { animation-delay: .4s; }
@keyframes bounce { 0%,60%,100% { transform: translateY(0); } 30% { transform: translateY(-8px); } }
.chat-status { display: flex; align-items: center; gap: var(--sp-2); padding: var(--sp-1) 0;
               color: var(--text-muted); font-size: 12px; font-style: italic; }
.chat-status .spinner { width: 14px; height: 14px; border: 2px solid var(--border);
                        border-top: 2px solid var(--accent); border-radius: 50%;
                        animation: spin .7s linear infinite; flex-shrink: 0; }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); }
                    to { opacity: 1; transform: translateY(0); } }
.welcome-msg {
  position: absolute; top: 50%; left: 50%;
  transform: translate(-50%, -50%);
  width: 100%; max-width: 560px;
  text-align: center; padding: var(--sp-6);
  color: var(--text-muted);
  animation: fadeIn .4s;
}
.welcome-msg h2 { font-size: 22px; color: var(--text-2); margin-bottom: var(--sp-2); }
.welcome-msg p { font-size: 14px; line-height: 1.8; }

/* ── 知识库管理 ── */
.kb-section {
  margin: var(--sp-2) var(--sp-3);
  padding: var(--sp-3);
  background: rgba(255,255,255,.03);
  border: 1px solid var(--sb-border);
  border-radius: var(--r-lg);
}
.kb-header { padding: 0 var(--sp-4) var(--sp-2); display: flex; justify-content: space-between;
             align-items: center; }
.kb-header h2 { font-size: 13px; color: var(--sb-text); font-weight: 600; }
.kb-stats { font-size: 11px; color: var(--sb-text-3); }
.kb-body { padding: 0 var(--sp-3) var(--sp-2); max-height: 200px; overflow-y: auto; }
.kb-file, .ds-item { min-height: 36px; padding: 8px 12px; }
.kb-file { display: flex; align-items: center; gap: var(--sp-1);
           border-radius: var(--r-sm); font-size: 12px; color: var(--sb-text);
           transition: background var(--t-fast); }
.kb-file:hover { background: var(--sb-bg-elev); }
.kb-file .kb-name { flex: 1; white-space: nowrap; overflow: hidden;
                   text-overflow: ellipsis; }
.kb-file .kb-badge { font-size: 10px; padding: 1px var(--sp-1); border-radius: var(--r-pill); flex-shrink: 0; }
.kb-badge.in { background: var(--success); color: var(--surface); }
.kb-badge.out { background: var(--text-2); color: var(--sb-text); }
.kb-del, .ds-del {
  min-width: 28px; min-height: 28px;
  padding: 4px 8px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: var(--r-sm);
  background: transparent; border: none; color: var(--sb-text-muted);
  cursor: pointer; font-size: 14px; flex-shrink: 0;
  transition: background var(--t-fast), color var(--t-fast);
}
.kb-del:hover, .ds-del:hover { background: rgba(233,69,96,.15); color: var(--accent); }
.kb-upload { padding: 0 var(--sp-4) var(--sp-2); }
.kb-upload input[type=file] { display: none; }
.kb-btn { width: 100%; padding: 7px; font-size: 12px; border-radius: var(--r-sm);
          border: 1px dashed var(--text-2); background: transparent; color: var(--sb-text-2);
          cursor: pointer; transition: all var(--t-fast); }
.kb-btn:hover { color: var(--accent); border-color: var(--accent); }
.kb-reindex { padding: 0 var(--sp-4) var(--sp-3); }
.kb-reindex .kb-btn { border-style: solid; font-size: 11px; }

/* ── 数据集管理 ── */
.ds-section {
  margin: var(--sp-2) var(--sp-3);
  padding: var(--sp-3);
  background: rgba(255,255,255,.03);
  border: 1px solid var(--sb-border);
  border-radius: var(--r-lg);
}
.ds-header { padding: 0 var(--sp-4) var(--sp-2); display: flex; justify-content: space-between; align-items: center; }
.ds-header h2 { font-size: 13px; color: var(--sb-text); font-weight: 600; }
.ds-count { font-size: 11px; color: var(--sb-text-3); }
.ds-body { padding: 0 var(--sp-3) var(--sp-2); max-height: 200px; overflow-y: auto; }
.ds-item { display: flex; align-items: center; gap: var(--sp-1);
           border-radius: var(--r-sm); font-size: 12px; color: var(--sb-text);
           transition: background var(--t-fast); cursor: pointer; }
.ds-item:hover { background: var(--sb-bg-elev); }
.ds-item .ds-icon { flex-shrink: 0; font-size: 14px; }
.ds-item .ds-name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ds-item .ds-rows { font-size: 10px; color: var(--sb-text-3); flex-shrink: 0; }
.ds-upload { padding: 0 var(--sp-4) var(--sp-2); }
.ds-upload input[type=file] { display: none; }
.ds-btn { width: 100%; padding: 7px; font-size: 12px; border-radius: var(--r-sm);
          border: 1px dashed var(--text-2); background: transparent; color: var(--sb-text-2);
          cursor: pointer; transition: all var(--t-fast); }
.ds-btn:hover { color: var(--accent); border-color: var(--accent); }
.ds-detail {
  max-height: 0; overflow: hidden;
  padding: 0 12px; background: var(--sb-bg-elev);
  border-radius: var(--r-sm); margin: 0 12px;
  font-size: 11px; color: var(--sb-text-2);
  transition: max-height var(--t-slow), padding var(--t-slow), margin var(--t-slow);
}
.ds-detail.show { max-height: 400px; padding: 8px 12px; margin: 4px 12px; }
.ds-detail table { width: 100%; font-size: 10px; border-collapse: collapse; }
.ds-detail th, .ds-detail td { padding: 2px var(--sp-1); text-align: left; border-bottom: 1px solid var(--sb-border); }
.ds-detail tbody tr:nth-child(even) { background: rgba(255,255,255,.03); }
.ds-detail tbody tr:hover { background: rgba(233,69,96,.15); }

/* ── 可折叠侧边栏分区 ── */
.ds-header, .kb-header { cursor: pointer; user-select: none; }
.section-chevron {
  display: inline-block; font-size: 10px; transition: transform var(--t-fast);
  margin-right: 4px; color: var(--sb-text-muted);
}
.section-body {
  max-height: 600px; overflow: hidden;
  transition: max-height var(--t-slow), opacity var(--t-med);
  opacity: 1;
}
.section-body.collapsed { max-height: 0; opacity: 0; }
.section-chevron.collapsed { transform: rotate(-90deg); }

/* ── 响应式 ── */
.hamburger { display: none; }
.sidebar-overlay {
  display: none; position: fixed; inset: 0;
  background: rgba(0,0,0,.5); z-index: 25;
  opacity: 0; transition: opacity var(--t-med);
}
.sidebar-overlay.show { display: block; opacity: 1; }

@media (max-width: 700px) {
  .hamburger {
    display: flex; align-items: center; justify-content: center;
    position: absolute; top: var(--sp-3); left: var(--sp-3);
    width: 40px; height: 40px; border-radius: var(--r-sm);
    background: var(--surface); border: 1px solid var(--border);
    box-shadow: var(--shadow-sm); font-size: 20px;
    color: var(--text); cursor: pointer; z-index: 10;
  }
  .sidebar {
    position: fixed; top: 0; left: 0; height: 100vh;
    transform: translateX(-100%); z-index: 30;
    transition: transform var(--t-slow);
  }
  .sidebar.open { transform: translateX(0); }
  .chat-container { padding-top: 64px; }
  .message { max-width: 92%; }
  .scroll-bottom-btn { right: var(--sp-4); bottom: 80px; }
}
/* ── 侧边栏滚动容器 ── */
.sidebar-scroll { flex: 1; overflow-y: auto; overflow-x: hidden; }
.sidebar-scroll::-webkit-scrollbar { width: 6px; }
.sidebar-scroll::-webkit-scrollbar-thumb { background: var(--sb-border); border-radius: var(--r-pill); }
.sidebar-scroll::-webkit-scrollbar-track { background: transparent; }
/* ── 代码块复制按钮 ── */
.copy-btn {
  position: absolute; top: var(--sp-2); right: var(--sp-2);
  padding: var(--sp-1) var(--sp-2);
  font-size: 11px; color: var(--code-fg);
  background: rgba(255,255,255,.08);
  border: 1px solid rgba(255,255,255,.15);
  border-radius: var(--r-sm);
  cursor: pointer; opacity: 0;
  transition: opacity var(--t-fast), background var(--t-fast);
}
.bubble pre:hover .copy-btn { opacity: 1; }
.copy-btn:hover { background: rgba(255,255,255,.18); }
.copy-btn.copied { background: var(--success); border-color: var(--success); }
.toast-container {
  position: fixed; top: var(--sp-4); right: var(--sp-4);
  display: flex; flex-direction: column; gap: var(--sp-2);
  z-index: 100; pointer-events: none;
}
.toast {
  min-width: 240px; padding: var(--sp-3) var(--sp-4);
  background: var(--surface); color: var(--text);
  border-radius: var(--r-md); border-left: 4px solid var(--accent);
  box-shadow: var(--shadow-lg); pointer-events: auto;
  font-size: 13px; animation: toastIn .3s ease;
}
.toast.success { border-left-color: var(--success); }
.toast.error { border-left-color: var(--danger); }
.toast.removing { animation: toastOut .3s ease forwards; }
@keyframes toastIn { from { opacity: 0; transform: translateX(40px); } to { opacity: 1; transform: translateX(0); } }
@keyframes toastOut { from { opacity: 1; transform: translateX(0); } to { opacity: 0; transform: translateX(40px); } }
.modal-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.4);
  backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
  display: none; align-items: center; justify-content: center;
  z-index: 200;
}
.modal-overlay.show { display: flex; }
.modal-card {
  background: var(--surface); border-radius: var(--r-lg);
  padding: var(--sp-5); max-width: 360px; width: 90%;
  box-shadow: var(--shadow-lg); animation: modalIn .2s ease;
}
@keyframes modalIn { from { opacity: 0; transform: scale(.95); } to { opacity: 1; transform: scale(1); } }
.modal-msg { font-size: 14px; color: var(--text); margin-bottom: var(--sp-4); line-height: 1.6; }
.modal-actions { display: flex; gap: var(--sp-2); justify-content: flex-end; }
.modal-btn {
  padding: var(--sp-2) var(--sp-4); border-radius: var(--r-sm);
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text); font-size: 13px; cursor: pointer;
  transition: background var(--t-fast), color var(--t-fast);
}
.modal-btn:hover { background: var(--surface-2); }
.modal-btn.ok { background: var(--accent); color: #fff; border-color: var(--accent); }
.modal-btn.ok:hover { background: var(--accent-hover); }

/* ── 收尾打磨 ── */
.avatar { font-size: 16px; line-height: 1; }
.sidebar-header h1 { display: flex; align-items: center; gap: var(--sp-2); }
.sidebar-header h1 .emoji, .sidebar-header .logo-emoji { font-size: 18px; line-height: 1; }
*:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--r-sm);
}
.chat-container::-webkit-scrollbar { width: 8px; }
.chat-container::-webkit-scrollbar-thumb { background: var(--border); border-radius: var(--r-pill); }
.chat-container::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }
.chat-container::-webkit-scrollbar-track { background: transparent; }
.typing-indicator span { background: var(--text-muted); }
.chat-status .spinner {
  border: 2px solid var(--border); border-top-color: var(--accent);
}
</style>
</head>
<body>

<!-- ── 侧边栏 ── -->
<div class="sidebar">
  <div class="sidebar-header">
    <h1><span class="logo-emoji">🤖</span> AI Data Analyst</h1>
    <div class="user-info" id="userDisplay"></div>
    <button class="btn-new-session" onclick="newSession()"><span>+ 新会话</span></button>
  </div>
  <div class="sidebar-scroll">
  <div class="session-list" id="sessionList">
    <div class="no-sessions">暂无会话记录</div>
  </div>
  <!-- ── 数据集管理 ── -->
  <div class="ds-section">
    <div class="ds-header" onclick="toggleSection('ds')">
      <h2><span class="section-chevron" id="chevronDs">▼</span> 📁 数据集</h2>
      <span class="ds-count" id="dsCount">-</span>
    </div>
    <div class="section-body" id="sectionBodyDs">
    <div class="ds-body" id="dsList">
      <div class="ds-item" style="color:var(--sb-text-muted);justify-content:center;">加载中...</div>
    </div>
    <div class="ds-upload">
      <input type="file" id="dsFileInput" accept=".csv,.xlsx,.xls">
      <button class="ds-btn" onclick="document.getElementById('dsFileInput').click()">＋ 上传 CSV/Excel</button>
    </div>
    </div>
  </div>
  <!-- ── 知识库管理（方案C） ── -->
  <div class="kb-section">
    <div class="kb-header" onclick="toggleSection('kb')">
      <h2><span class="section-chevron" id="chevronKb">▼</span> 📚 知识库</h2>
      <span class="kb-stats" id="kbStats">-</span>
    </div>
    <div class="section-body" id="sectionBodyKb">
    <div class="kb-body" id="kbFileList">
      <div class="kb-file" style="color:var(--sb-text-muted);justify-content:center;">加载中...</div>
    </div>
    <div class="kb-upload">
      <input type="file" id="kbFileInput" multiple accept=".txt,.pdf,.docx,.md">
      <button class="kb-btn" onclick="document.getElementById('kbFileInput').click()">＋ 上传并入库</button>
    </div>
    <div class="kb-reindex">
      <button class="kb-btn" onclick="kbReindex()">⟳ 全量重建索引</button>
    </div>
    </div>
  </div>
  </div>
  <div class="sidebar-footer">
    <button class="btn-logout" onclick="logout()"><span>登出</span></button>
  </div>
</div>
<div class="sidebar-overlay" id="sidebarOverlay" onclick="closeSidebar()"></div>

<!-- ── 主内容区 ── -->
<div class="main-area">
  <button class="hamburger" id="hamburgerBtn" onclick="toggleSidebar()" aria-label="菜单">☰</button>
  <div class="chat-container" id="chatContainer">
    <div class="welcome-msg">
      <h2>👋 你好，我是 AI 数据分析顾问</h2>
      <p>我可以帮你分析销售趋势、产品表现、利润变化，生成图表和报告。<br>
      也可以回答知识库问题、查询外部数据。<br><br>
      请直接描述你的需求，我会自动选择合适的分析方式。</p>
    </div>
  </div>
  <button class="scroll-bottom-btn" id="scrollBottomBtn" onclick="scrollToBottom()" aria-label="滚动到底部" title="滚动到底部">↓</button>
  <div class="input-area">
    <div class="input-row">
      <input type="text" id="userInput" placeholder="请输入你的问题..."
             onkeypress="if(event.key==='Enter') sendMessage()" autofocus>
      <button id="sendBtn" onclick="sendMessage()">发送</button>
    </div>
  </div>
</div>

<script>
let isProcessing = false;
let authToken = sessionStorage.getItem('token') || '';
let accountName = sessionStorage.getItem('account') || '';
let currentSessionId = '';

if (!authToken) { window.location.href = '/'; }

document.getElementById('userDisplay').textContent = '👤 ' + accountName;

// ── 可折叠侧边栏分区 ──
function toggleSection(name) {
  var body = document.getElementById('sectionBody' + (name === 'ds' ? 'Ds' : 'Kb'));
  var chevron = document.getElementById('chevron' + (name === 'ds' ? 'Ds' : 'Kb'));
  if (!body || !chevron) return;
  body.classList.toggle('collapsed');
  chevron.classList.toggle('collapsed');
}

// ── 移动端抽屉 ──
function toggleSidebar() {
  var sb = document.querySelector('.sidebar');
  var ov = document.getElementById('sidebarOverlay');
  sb.classList.toggle('open');
  ov.classList.toggle('show');
}
function closeSidebar() {
  document.querySelector('.sidebar').classList.remove('open');
  document.getElementById('sidebarOverlay').classList.remove('show');
}

// ── 加载侧边栏会话列表 ──
async function loadSessions() {
  try {
    const r = await fetch('/api/sessions', {
      headers: {'Authorization': 'Bearer ' + authToken}
    });
    if (r.ok) {
      const data = await r.json();
      renderSessionList(data.sessions || []);
    }
  } catch(e) { console.log('加载会话列表失败:', e); }
}

function renderSessionList(sessions) {
  const list = document.getElementById('sessionList');
  if (sessions.length === 0) {
    list.innerHTML = '<div class="no-sessions">暂无会话记录</div>';
    return;
  }
  list.innerHTML = sessions.map(s => {
    const active = s.session_id === currentSessionId ? ' active' : '';
    const date = new Date(s.updated_at || s.created_at);
    const timeStr = date.toLocaleDateString('zh-CN') + ' ' +
                    date.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'});
    return `<div class="session-item${active}" data-sid="${s.session_id}" onclick="switchSession('${s.session_id}')">
      <div class="s-title">${escapeHtml(s.title || '未命名会话')}</div>
      <div class="s-time">${timeStr}</div>
    </div>`;
  }).join('');
}

// ── 切换到指定会话 ──
async function switchSession(sessionId) {
  if (window.innerWidth <= 700) { closeSidebar(); }
  if (currentSessionId === sessionId) return;
  currentSessionId = sessionId;
  updateActiveSession();
  const container = document.getElementById('chatContainer');
  container.innerHTML = '<div class="typing-indicator" style="justify-content:center;padding:40px"><span></span><span></span><span></span></div>';

  try {
    const r = await fetch('/api/sessions/' + sessionId, {
      headers: {'Authorization': 'Bearer ' + authToken}
    });
    if (r.ok) {
      const data = await r.json();
      const msgs = data.conversation || [];
      container.innerHTML = '';
      if (msgs.length === 0) {
        container.innerHTML = '<div class="welcome-msg"><p>该会话暂无消息</p></div>';
      } else {
        msgs.forEach(m => {
          const role = m.role === 'user' ? 'user' : 'assistant';
          appendMessage(role, m.content || '');
        });
      }
      scrollToBottom();
    } else {
      container.innerHTML = '<div class="welcome-msg"><p>加载会话失败</p></div>';
    }
  } catch(e) {
    container.innerHTML = '<div class="welcome-msg"><p>加载会话失败: ' + e.message + '</p></div>';
  }
}

function updateActiveSession() {
  document.querySelectorAll('.session-item').forEach(el => {
    el.classList.toggle('active', el.dataset.sid === currentSessionId);
  });
}

// ── 新建会话 ──
function newSession() {
  currentSessionId = '';
  updateActiveSession();
  const container = document.getElementById('chatContainer');
  container.innerHTML = `<div class="welcome-msg">
    <h2>👋 新会话已开始</h2>
    <p>请输入你的问题，我会为你进行分析。</p>
  </div>`;
  document.getElementById('userInput').focus();
}

function logout() {
  fetch('/api/logout', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + authToken}
  });
  sessionStorage.removeItem('token');
  sessionStorage.removeItem('account');
  window.location.href = '/';
}

function authHeaders() {
  return {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + authToken};
}

async function sendMessage() {
  if (isProcessing) return;
  const input = document.getElementById('userInput');
  const text = input.value.trim();
  if (!text) return;

  isProcessing = true;
  input.value = '';
  document.getElementById('sendBtn').disabled = true;

  // 移除欢迎消息
  const welcome = document.querySelector('.welcome-msg');
  if (welcome) welcome.remove();

  appendMessage('user', text);

  const assistantMsg = appendMessage('assistant', '');
  const bubble = assistantMsg.querySelector('.bubble');
  bubble.innerHTML = '<div class="typing-indicator"><span></span><span></span><span></span></div>';
  scrollToBottom();

  try {
    await streamChat(text, bubble);
  } catch (err) {
    bubble.innerHTML = `<span style="color:#c53030">请求失败: ${err.message}</span>`;
  } finally {
    isProcessing = false;
    document.getElementById('sendBtn').disabled = false;
    document.getElementById('userInput').focus();
  }
}

async function streamChat(text, bubble) {
  const body = { query: text };
  if (currentSessionId) body.session_id = currentSessionId;

  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(body),
  });

  if (!response.ok) throw new Error(`HTTP ${response.status}`);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let fullText = '';
  let thinking = true; // 默认为思考状态
  let statusEl = null; // 思考状态 DOM 元素

  // 查找当前消息的 status 行
  const msgDiv = bubble.closest('.message');
  if (msgDiv) {
    statusEl = msgDiv.querySelector('.chat-status');
    if (statusEl) {
      statusEl.style.display = 'flex';
      statusEl.querySelector('.status-text').textContent = 'AI 正在思考...';
    }
  }

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const chunk = decoder.decode(value, { stream: true });
    const lines = chunk.split('\\n');
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const data = line.slice(6);

      if (data === '[DONE]') continue;

      if (data.startsWith('[ERROR]')) {
        bubble.innerHTML = `<span style="color:#c53030">${escapeHtml(data.slice(7))}</span>`;
        if (statusEl) statusEl.style.display = 'none';
        return;
      }

      if (data.startsWith('[THINKING]')) {
        const status = data.slice(10);
        if (statusEl) {
          statusEl.style.display = 'flex';
          statusEl.querySelector('.status-text').textContent = escapeHtml(status);
        }
        scrollToBottom();
        continue;
      }

      if (data.startsWith('[SESSION]')) {
        currentSessionId = data.slice(9);
        updateActiveSession();
        continue;
      }

      if (data === '[SESSIONS_RELOAD]') {
        loadSessions();
        continue;
      }

      if (data.startsWith('[CHART:')) {
        const chartUrl = data.slice(7, -1).trim();
        // XSS 防护：图表 URL 必须是站内相对路径（以 / 开头），拒绝 javascript:/外部 http
        if (chartUrl && chartUrl.charAt(0) === '/' && !chartUrl.startsWith('//')) {
          const iframe = document.createElement('iframe');
          iframe.src = chartUrl;
          iframe.style.cssText = 'width:100%;height:400px;border:none;border-radius:8px;margin:8px 0;';
          const wrapper = document.createElement('div');
          if (!wrapper.dataset.created) {
            wrapper.dataset.created = '1';
            wrapper.appendChild(iframe);
            bubble.appendChild(wrapper);
          }
        }
        continue;
      }

      if (data.startsWith('[CONTEXT]')) continue;

      if (data.startsWith('[AUDIT:')) {
        fullText += '\\n> 📋 ' + data.slice(7, -1).trim() + '\\n';
        // 首次收到实际内容时，关闭思考和转圈
        if (thinking) {
          thinking = false;
          if (statusEl) statusEl.style.display = 'none';
          bubble.innerHTML = '';
        }
        bubble.innerHTML = renderMarkdown(fullText);
        scrollToBottom();
        continue;
      }

      // 正常内容：流式追加
      if (thinking) {
        // 首次收到实际内容：关闭思考状态和转圈
        thinking = false;
        if (statusEl) statusEl.style.display = 'none';
        bubble.innerHTML = ''; // 清除 typing indicator
      }
      if (fullText.length > 0) fullText += '\\n';
      fullText += data;
      bubble.innerHTML = renderMarkdown(fullText);
      scrollToBottom();
    }
  }

  // 处理完成后的状态
  if (statusEl) statusEl.style.display = 'none';

  if (!fullText.trim()) {
    bubble.innerHTML = '收到空响应，请重试。';
    if (statusEl) statusEl.style.display = 'none';
  }
}

function appendMessage(role, text) {
  var container = document.getElementById('chatContainer');
  var div = document.createElement('div');
  div.className = 'message ' + role;
  var statusDiv = role === 'assistant'
    ? '<div class="chat-status" style="display:none"><span class="spinner"></span><span class="status-text"></span></div>'
    : '';
  var now = new Date();
  var ts = now.toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
  div.innerHTML = '<div class="avatar">' + (role === 'user' ? '👤' : '🤖') + '</div>'
    + '<div class="bubble-wrap"><div class="bubble">' + escapeHtml(text) + '</div>'
    + '<div class="msg-meta">' + ts + '</div></div>' + statusDiv;
  container.appendChild(div);
  return div;
}

function renderMarkdown(text) {
  // XSS 防护：先对整段原始文本做 HTML 转义，使 LLM 输出中的 <script>/<img onerror>
  // 等字面量标签失效，再做 markdown 语法替换。代码块内容亦已转义，无需二次转义。
  let html = escapeHtml(text);
  // 协议白名单：仅放行 http(s) 与相对路径，拦截 javascript:/data: 等
  function safeUrl(u) {
    var s = (u || '').trim();
    if (/^(https?:|\/|\.\/|\.\.\/|#)/i.test(s)) return s;
    return '';
  }
  // 代码块（内容已转义，直接包裹）
  html = html.replace(/```(\\w*)\\n([\\s\\S]*?)```/g, function(_, lang, code) {
    return '<pre><button class="copy-btn" onclick="copyCode(this)">复制</button><code>' + code.trim() + '</code></pre>';
  });
  // 标题
  html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  // 粗体/斜体
  html = html.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
  html = html.replace(/\\*(.+?)\\*/g, '<em>$1</em>');
  // 行内代码
  html = html.replace(/`(.+?)`/g, '<code>$1</code>');
  // 分隔线
  html = html.replace(/^---+$/gm, '<hr>');
  // 图片（协议白名单，非 http(s)/相对路径则丢弃 src）
  html = html.replace(/!\\[(.*?)\\]\\((.*?)\\)/g, function(_, alt, url) {
    var u = safeUrl(url); return u ? '<img src="' + u + '" alt="' + alt + '">' : alt;
  });
  // 链接（协议白名单）
  html = html.replace(/\\[(.*?)\\]\\((.*?)\\)/g, function(_, label, url) {
    var u = safeUrl(url);
    return u ? '<a href="' + u + '">' + label + '</a>' : label;
  });
  // 无序列表
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\\/li>)/s, '<ul>$1</ul>');
  // 有序列表
  html = html.replace(/^\\d+\\. (.+)$/gm, '<li>$1</li>');
  // 引用
  html = html.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
  // 段落
  html = html.replace(/\\n\\n/g, '</p><p>');
  html = '<p>' + html + '</p>';
  html = html.replace(/<p><\\/p>/g, '');
  html = html.replace(/<p>(<[hHuol])/g, '$1');
  html = html.replace(/(<\\/[hH]\\d>|<\\/[uo]l>)<\\/p>/g, '$1');
  html = html.replace(/\\n/g, '<br>');
  return html;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function copyCode(btn) {
  var code = btn.parentElement.querySelector('code');
  if (!code) return;
  navigator.clipboard.writeText(code.textContent).then(function() {
    var orig = btn.textContent;
    btn.textContent = '已复制'; btn.classList.add('copied');
    setTimeout(function() { btn.textContent = orig; btn.classList.remove('copied'); }, 1500);
  }).catch(function() { btn.textContent = '复制失败'; setTimeout(function() { btn.textContent = '复制'; }, 1500); });
}

function scrollToBottom() {
  var container = document.getElementById('chatContainer');
  setTimeout(function() { container.scrollTop = container.scrollHeight; }, 50);
}

var _chatContainer = document.getElementById('chatContainer');
var _scrollBottomBtn = document.getElementById('scrollBottomBtn');
_chatContainer.addEventListener('scroll', function() {
  var atBottom = _chatContainer.scrollHeight - _chatContainer.scrollTop - _chatContainer.clientHeight < 80;
  _scrollBottomBtn.classList.toggle('show', !atBottom);
});

// ── 知识库管理（方案C） ──
function fmtSize(bytes) {
  if (bytes < 1024) return bytes + 'B';
  if (bytes < 1048576) return (bytes/1024).toFixed(1) + 'KB';
  return (bytes/1048576).toFixed(1) + 'MB';
}

async function loadKbFiles() {
  try {
    const r = await fetch('/api/knowledge/files', {headers: authHeaders()});
    if (!r.ok) { document.getElementById('kbFileList').innerHTML = '<div class="kb-file" style="color:#718096;justify-content:center;">加载失败</div>'; return; }
    const data = await r.json();
    const files = data.files || [];
    const list = document.getElementById('kbFileList');
    if (files.length === 0) {
      list.innerHTML = '<div class="kb-file" style="color:#718096;justify-content:center;">暂无知识库文件</div>';
    } else {
      list.innerHTML = files.map(f => {
        const badge = f.ingested
          ? '<span class="kb-badge in">已入库</span>'
          : '<span class="kb-badge out">待入库</span>';
        return `<div class="kb-file" title="${escapeHtml(f.filename)}">
          <span class="kb-name">${escapeHtml(f.filename)}</span>
          ${badge}
          <button class="kb-del" onclick="deleteKbFile('${escapeHtml(f.filename)}')" title="删除">✕</button>
        </div>`;
      }).join('');
    }
    // 统计
    loadKbStats();
  } catch(e) {
    console.log('加载知识库列表失败:', e);
  }
}

async function loadKbStats() {
  try {
    const r = await fetch('/api/knowledge/stats', {headers: authHeaders()});
    if (r.ok) {
      const s = await r.json();
      document.getElementById('kbStats').textContent =
        (s.total_sources || 0) + '文件/' + (s.total_chunks || 0) + '分片';
    }
  } catch(e) {}
}

// 文件上传入库
document.getElementById('kbFileInput').addEventListener('change', async (e) => {
  const files = e.target.files;
  if (!files || files.length === 0) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  try {
    const r = await fetch('/api/knowledge/upload', {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + authToken},
      body: fd
    });
    const data = await r.json();
    const ok = (data.results || []).filter(x => x.success).length;
    showToast('上传完成：成功 ' + ok + ' / ' + files.length + ' 个文件', 'success');
    loadKbFiles();
  } catch(err) { showToast('上传失败: ' + err.message, 'error', 4000); }
  e.target.value = '';
});

async function deleteKbFile(filename) {
  if (!(await showConfirm('确认删除知识库文件及其分片？\n' + filename))) return;
  try {
    const r = await fetch('/api/knowledge/files/' + encodeURIComponent(filename), {
      method: 'DELETE', headers: authHeaders()
    });
    const data = await r.json();
    if (data.success !== undefined && !data.success) {
      showToast(data.error || '删除失败', 'error', 4000); return;
    }
    loadKbFiles();
  } catch(e) { showToast('删除失败: ' + e.message, 'error', 4000); }
}

async function kbReindex() {
  if (!(await showConfirm('全量重建将清空当前向量库并重新入库所有文件，耗时较长。确认继续？'))) return;
  try {
    const r = await fetch('/api/knowledge/reindex', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify({confirm: true})
    });
    const data = await r.json();
    if (data.error) { showToast(data.error, 'error', 4000); return; }
    showToast('重建完成：重载 ' + (data.reloaded_files || 0) + ' 个文件，共 ' +
          ((data.stats && data.stats.total_chunks) || 0) + ' 个分片', 'success');
    loadKbFiles();
  } catch(e) { showToast('重建失败: ' + e.message, 'error', 4000); }
}

// ── 数据集管理 ──
function dsIcon(type) {
  if (type === 'csv') return '📄';
  if (type === 'excel') return '📊';
  if (type === 'mysql') return '🗄️';
  if (type === 'postgres') return '🐘';
  return '📁';
}

async function loadDatasets() {
  try {
    const r = await fetch('/api/datasets', {headers: authHeaders()});
    if (!r.ok) { document.getElementById('dsList').innerHTML = '<div class="ds-item" style="color:#718096;justify-content:center;">加载失败</div>'; return; }
    const data = await r.json();
    const datasets = data.datasets || [];
    document.getElementById('dsCount').textContent = datasets.length + ' 个';
    const list = document.getElementById('dsList');
    if (datasets.length === 0) {
      list.innerHTML = '<div class="ds-item" style="color:#718096;justify-content:center;">暂无数据集</div>';
    } else {
      list.innerHTML = datasets.map(d => {
        const rows = d.row_count > 0 ? d.row_count.toLocaleString() + '行' : '';
        const safeId = String(d.name).replace(/[^A-Za-z0-9_]/g,'_');
        return `<div class="ds-item" onclick="toggleDsDetail('${safeId}')">
          <span class="ds-icon">${dsIcon(d.source_type)}</span>
          <span class="ds-name" title="${escapeHtml(d.name)}">${escapeHtml(d.name)}</span>
          <span class="ds-rows">${rows}</span>
          <button class="ds-del" onclick="event.stopPropagation();deleteDs('${escapeHtml(d.name)}')" title="删除">✕</button>
        </div>
        <div class="ds-detail" id="ds-detail-${safeId}">加载中...</div>`;
      }).join('');
    }
  } catch(e) { console.log('加载数据集失败:', e); }
}

async function toggleDsDetail(name) {
  const el = document.getElementById('ds-detail-' + name);
  if (!el) return;
  if (el.classList.contains('show')) { el.classList.remove('show'); return; }
  el.classList.add('show');
  try {
    const r = await fetch('/api/datasets/' + encodeURIComponent(name) + '/schema', {headers: authHeaders()});
    if (r.ok) {
      const d = await r.json();
      const cols = (d.columns || []).map(c => `<tr><td>${escapeHtml(c.name)}</td><td>${escapeHtml(c.type)}</td></tr>`).join('');
      el.innerHTML = `<strong>${escapeHtml(d.name)}</strong> (${d.source_type}, ${d.row_count}行)<table><tr><th>列名</th><th>类型</th></tr>${cols}</table>`;
    } else {
      el.innerHTML = '加载失败';
    }
  } catch(e) { el.innerHTML = '加载失败: ' + e.message; }
}

// 数据集上传
document.getElementById('dsFileInput').addEventListener('change', async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await fetch('/api/datasets/upload', {
      method: 'POST',
      headers: {'Authorization': 'Bearer ' + authToken},
      body: fd
    });
    const data = await r.json();
    if (data.success) {
      showToast('已加载数据集「' + data.name + '」，' + data.row_count + ' 行，' + data.columns.length + ' 列', 'success');
    } else {
      showToast('上传失败: ' + (data.error || '未知错误'), 'error', 4000);
    }
    loadDatasets();
  } catch(err) { showToast('上传失败: ' + err.message, 'error', 4000); }
  e.target.value = '';
});

async function deleteDs(name) {
  if (!(await showConfirm('确认删除数据集「' + name + '」？\n将同时删除 DuckDB 表和本地文件。'))) return;
  try {
    const r = await fetch('/api/datasets/' + encodeURIComponent(name), {
      method: 'DELETE', headers: authHeaders()
    });
    const data = await r.json();
    if (data.success) { loadDatasets(); }
    else { showToast(data.error || '删除失败', 'error', 4000); }
  } catch(e) { showToast('删除失败: ' + e.message, 'error', 4000); }
}

loadDatasets();

// ── Toast / Modal ──
function showToast(msg, type, duration) {
  type = type || 'info';
  duration = duration || 3000;
  var container = document.getElementById('toastContainer');
  var toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(function() {
    toast.classList.add('removing');
    setTimeout(function() { toast.remove(); }, 300);
  }, duration);
}

var _modalResolve = null;
function showConfirm(msg) {
  return new Promise(function(resolve) {
    _modalResolve = resolve;
    document.getElementById('modalMsg').textContent = msg;
    document.getElementById('modalOverlay').classList.add('show');
  });
}
function resolveModal(result) {
  document.getElementById('modalOverlay').classList.remove('show');
  if (_modalResolve) { _modalResolve(result); _modalResolve = null; }
}

// ── 初始化 ──
loadSessions();
loadKbFiles();
document.getElementById('userInput').focus();
</script>
<div class="toast-container" id="toastContainer"></div>
<div class="modal-overlay" id="modalOverlay">
  <div class="modal-card">
    <div class="modal-msg" id="modalMsg"></div>
    <div class="modal-actions">
      <button class="modal-btn cancel" onclick="resolveModal(false)">取消</button>
      <button class="modal-btn ok" onclick="resolveModal(true)">确认</button>
    </div>
  </div>
</div>
</body>
</html>"""


# ── Routes ──

@app.post("/api/register")
async def api_register(request: Request):
    """用户注册。注册成功后自动登录并返回 token。"""
    body = await request.json()
    account = body.get("account", "").strip()
    password = body.get("password", "")
    result = user_db.register(account, password)
    if result.get("success"):
        # 注册成功后自动登录
        try:
            login_result = user_db.login(account, password)
            if login_result.get("success"):
                return JSONResponse(content={
                    "success": True,
                    "user_id": login_result.get("user_id"),
                    "account": login_result.get("account"),
                    "token": login_result.get("token"),
                })
            else:
                return JSONResponse(content={
                    "success": True,
                    "message": "注册成功，但自动登录失败，请手动登录",
                })
        except Exception as e:
            logger.error(f"Auto-login after registration failed: {e}")
            return JSONResponse(content={
                "success": True,
                "message": "注册成功，请手动登录",
            })
    return JSONResponse(content=result, status_code=400)


@app.post("/api/login")
async def api_login(request: Request):
    """用户登录。返回 token。"""
    body = await request.json()
    account = body.get("account", "").strip()
    password = body.get("password", "")
    result = user_db.login(account, password)
    if result.get("success"):
        return JSONResponse(content=result)
    return JSONResponse(content=result, status_code=401)


@app.post("/api/logout")
async def api_logout(request: Request):
    """用户登出。"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        user_db.logout(token)
    return JSONResponse(content={"success": True})


@app.get("/", response_class=HTMLResponse)
async def index():
    """返回登录页面。"""
    return HTMLResponse(content=LOGIN_PAGE)


@app.get("/app", response_class=HTMLResponse)
async def app_page():
    """返回主应用页面。"""
    return HTMLResponse(content=HTML_TEMPLATE)


@app.post("/api/chat")
async def api_chat(request: Request):
    """统一智能客服：流式 SSE 响应（带会话管理、记忆管理、自动调度分析 Agent）。"""
    body = await request.json()
    query = body.get("query", "").strip()
    session_id = body.get("session_id", "").strip()
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    memory = get_session(user_id)

    # ── 获取历史上下文（必须在 add_user_message 之前，避免当前消息重复） ──
    mem_context = memory.get_context(max_turns=10)
    memory.add_user_message(query)

    # ── 会话管理：无 session_id 则创建新会话 ──
    new_session = False
    if not session_id:
        title = query[:30] + ("..." if len(query) > 30 else "")
        session_id = _long_term_memory.create_session(user_id, title=title)
        new_session = True
    else:
        # IDOR 防护：传入的 session 必须属于当前用户，否则拒绝（防写入/读取他人会话）
        owner = _long_term_memory.get_session_owner(session_id)
        if owner is None or owner != user_id:
            return JSONResponse({"error": "会话不存在或无权访问"}, status_code=404)
        _long_term_memory.touch_session(session_id)

    agent = _get_react_agent()

    async def generate() -> AsyncGenerator[str, None]:
        full_response = ""
        # ── 记录分析前已有的图表文件，用于后续检测新图表 ──
        charts_dir = get_abs_path("reports/charts")
        existing_charts = set()
        if os.path.isdir(charts_dir):
            for f in os.listdir(charts_dir):
                if f.endswith(".html"):
                    existing_charts.add(os.path.join(charts_dir, f))
        try:
            # 通知前端 session_id
            yield f"data: [SESSION]{session_id}\n\n"
            if new_session:
                yield f"data: [SESSIONS_RELOAD]\n\n"

            for chunk in agent.execute_stream(query, history=mem_context,
                                              user_id=user_id, session_id=session_id):
                if not chunk:
                    continue
                stripped = chunk.strip()
                # 思考状态指示：立即透传
                if stripped.startswith("[THINKING]"):
                    yield f"data: [THINKING]{stripped[10:]}\n\n"
                    continue
                full_response += chunk
                # ── 流式：按句子拆分，逐个发送 ──
                sentences = _split_sentences(stripped)
                if sentences:
                    for sentence in sentences:
                        yield f"data: {sentence.strip()}\n\n"
                        await asyncio.sleep(0.06)
                else:
                    # 无法拆分的内容（如列表项、标题等）原样输出
                    yield f"data: {stripped}\n\n"
                    await asyncio.sleep(0.03)

            # ── 检测新生成的图表文件并发送给前端 ──
            if os.path.isdir(charts_dir):
                for f in sorted(os.listdir(charts_dir)):
                    if f.endswith(".html"):
                        fpath = os.path.join(charts_dir, f)
                        if fpath not in existing_charts:
                            web_url = _to_web_path(fpath)
                            yield f"data: [CHART:{web_url}]\n\n"

            # 存入短期 + 长期记忆
            cleaned = full_response.strip()
            if cleaned:
                memory.add_assistant_message(cleaned)
                try:
                    _long_term_memory.save_conversation_pair(
                        user_id, query, cleaned, session_id=session_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to save conversation to long-term memory: {e}")
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Chat streaming error: {e}")
            yield f"data: [ERROR] {str(e)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/analysis")
async def api_analysis(request: Request):
    """数据分析：同步返回 JSON（带记忆管理）。"""
    body = await request.json()
    query = body.get("query", "").strip()
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    memory = get_session(user_id)
    memory.add_user_message(query)

    try:
        analyst = _get_planner_agent()
        result = analyst.run({"query": query, "user_id": user_id})

        # 将分析结果摘要存入记忆
        report = result.get("report", {})
        summary_text = report.get("markdown", str(result.get("title", "")))
        if summary_text:
            memory.add_assistant_message(f"[分析结果] {summary_text[:500]}")

        # 序列化时处理非 JSON 兼容类型 + 转换图表路径为 Web 可访问
        result = _sanitize_result(result)
        result = _normalize_paths(result)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Analysis error: {traceback.format_exc()}")
        return JSONResponse(
            {"success": False, "errors": [str(e)]},
            status_code=500,
        )


@app.get("/api/conversation/history")
async def api_conversation_history(request: Request, limit: int = 20):
    """获取用户历史会话记录（长期记忆）。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    turns = _long_term_memory.get_last_n_turns(user_id, n=limit)
    return JSONResponse(content={"user_id": user_id, "turns": turns, "count": len(turns)})


@app.get("/api/sessions")
async def api_list_sessions(request: Request):
    """获取用户的所有会话列表（按最近活跃排序）。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    sessions = _long_term_memory.get_user_sessions(user_id)
    return JSONResponse(content={"user_id": user_id, "sessions": sessions, "count": len(sessions)})


@app.get("/api/sessions/{session_id}")
async def api_get_session(request: Request, session_id: str):
    """获取指定会话的完整对话历史。

    IDOR 防护：校验该会话归属当前用户，拒绝读取他人会话。
    """
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    # 归属校验：会话不存在或不属于当前用户一律 404（避免枚举）
    owner = _long_term_memory.get_session_owner(session_id)
    if owner is None or owner != user_id:
        return JSONResponse({"error": "会话不存在或无权访问"}, status_code=404)
    conversation = _long_term_memory.get_session_conversation(session_id)
    return JSONResponse(content={
        "session_id": session_id,
        "user_id": user_id,
        "conversation": conversation,
        "count": len(conversation),
    })


@app.get("/api/health")
async def health():
    """健康检查。"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ── 数据集管理 ──

def _datasets_dir() -> str:
    """用户上传的数据集存放目录。"""
    d = get_abs_path("data/datasets")
    os.makedirs(d, exist_ok=True)
    return d

_ALLOWED_DATASET_TYPES = {"csv", "xlsx", "xls"}
_MAX_DATASET_SIZE = 100 * 1024 * 1024  # 100MB


@app.get("/api/datasets")
async def list_datasets(request: Request):
    """列出所有可用数据集。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    try:
        from database.datasources_db import datasources_db
    except ModuleNotFoundError:
        from agent.database.datasources_db import datasources_db
    datasets = datasources_db.list_datasets(owner_user_id=user_id)
    return JSONResponse({"datasets": datasets, "count": len(datasets)})


@app.post("/api/datasets/upload")
async def upload_dataset(request: Request, file: UploadFile = File(...)):
    """上传 CSV/Excel 文件，解析并加载到 DuckDB。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)

    fname = os.path.basename(file.filename or "")
    ext = os.path.splitext(fname)[1].lower().lstrip(".")
    if ext not in _ALLOWED_DATASET_TYPES:
        return JSONResponse(
            {"success": False, "error": f"不支持的文件类型: {ext}，仅支持 CSV/XLSX/XLS"},
            status_code=400,
        )

    content = await file.read()
    if len(content) > _MAX_DATASET_SIZE:
        return JSONResponse(
            {"success": False, "error": f"文件超过大小限制(100MB)"},
            status_code=413,
        )

    # 保存文件
    ds_dir = _datasets_dir()
    # 生成安全的表名：文件名去扩展名，替换非法字符
    base_name = os.path.splitext(fname)[0]
    safe_name = re_module.sub(r'[^A-Za-z0-9]+', '_', base_name).strip('_')
    if not safe_name or not safe_name[0].isalpha():
        safe_name = "ds_" + (safe_name or "upload")

    # 处理同名冲突
    try:
        from database.datasources_db import datasources_db
    except ModuleNotFoundError:
        from agent.database.datasources_db import datasources_db

    table_name = safe_name
    counter = 2
    while datasources_db.get_dataset(table_name, owner_user_id=user_id):
        table_name = f"{safe_name}_{counter}"
        counter += 1

    fpath = os.path.join(ds_dir, f"{table_name}.{ext}")
    with open(fpath, "wb") as out:
        out.write(content)

    # 加载到 DuckDB
    try:
        from database.duckdb_manager import init_duckdb, safe_ident
    except ModuleNotFoundError:
        from agent.database.duckdb_manager import init_duckdb, safe_ident

    try:
        db = init_duckdb(user_id=user_id)
        if ext == "csv":
            load_result = db.load_csv_dataset(fpath, table_name)
        else:
            load_result = db.load_excel_dataset(fpath, table_name)

        if not load_result["success"]:
            # 加载失败，删除文件
            os.remove(fpath)
            return JSONResponse({"success": False, "error": load_result["error"]}, status_code=400)

        # 解析 schema
        qname = safe_ident(table_name)
        cols = db.execute(f"DESCRIBE {qname}").fetchall()
        schema_json = json.dumps([
            {"name": c[0], "type": c[1]} for c in cols
        ], ensure_ascii=False)

        # 获取样本数据（前5行）
        try:
            sample_df = db.query_df(f"SELECT * FROM {qname} LIMIT 5")
            sample_data = sample_df.to_dict(orient="records")
        except Exception:
            sample_data = []

        # 写入元数据（带 owner_user_id 实现多用户隔离）
        source_type = "csv" if ext == "csv" else "excel"
        datasources_db.add_dataset(
            name=table_name,
            source_type=source_type,
            file_path=fpath,
            table_name=table_name,
            schema_json=schema_json,
            row_count=load_result["row_count"],
            owner_user_id=user_id,
        )

        return JSONResponse({
            "success": True,
            "name": table_name,
            "source_type": source_type,
            "row_count": load_result["row_count"],
            "columns": [c[0] for c in cols],
            "sample": sample_data,
        })

    except Exception as e:
        logger.error(f"Dataset upload failed: {traceback.format_exc()}")
        if os.path.exists(fpath):
            os.remove(fpath)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.delete("/api/datasets/{name}")
async def delete_dataset(request: Request, name: str):
    """删除数据集（卸载 DuckDB 表 + 删除文件 + 删除元数据）。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    if "/" in name or "\\" in name or name in ("", ".", ".."):
        return JSONResponse({"error": "非法数据集名"}, status_code=400)

    try:
        from database.datasources_db import datasources_db
    except ModuleNotFoundError:
        from agent.database.datasources_db import datasources_db

    ds = datasources_db.get_dataset(name, owner_user_id=user_id)
    if not ds:
        return JSONResponse({"error": f"数据集 '{name}' 不存在或不属于当前用户"}, status_code=404)

    # 从 DuckDB 删除表
    try:
        from database.duckdb_manager import init_duckdb
    except ModuleNotFoundError:
        from agent.database.duckdb_manager import init_duckdb

    try:
        db = init_duckdb(user_id=user_id)
        db.drop_table(ds["table_name"])
    except Exception as e:
        logger.warning(f"Failed to drop table {ds['table_name']}: {e}")

    # 删除文件（路径穿越防护：仅允许删除 datasets 目录下的文件）
    if ds["file_path"] and os.path.exists(ds["file_path"]):
        try:
            allowed_dir = os.path.abspath(_datasets_dir())
            real_path = os.path.realpath(ds["file_path"])
            if real_path.startswith(allowed_dir + os.sep):
                os.remove(real_path)
            else:
                logger.warning(f"Refusing to delete file outside datasets dir: {real_path}")
        except Exception as e:
            logger.warning(f"Failed to delete file {ds['file_path']}: {e}")

    # 删除元数据（带归属校验，防越权）
    datasources_db.delete_dataset(name, owner_user_id=user_id)
    return JSONResponse({"success": True})


@app.get("/api/datasets/{name}/schema")
async def get_dataset_schema(request: Request, name: str):
    """获取数据集的详细 schema。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)

    try:
        from database.datasources_db import datasources_db
    except ModuleNotFoundError:
        from agent.database.datasources_db import datasources_db

    ds = datasources_db.get_dataset(name, owner_user_id=user_id)
    if not ds:
        return JSONResponse({"error": f"数据集 '{name}' 不存在或不属于当前用户"}, status_code=404)

    # 从 DuckDB 获取实时 schema
    try:
        from database.duckdb_manager import init_duckdb, safe_ident
    except ModuleNotFoundError:
        from agent.database.duckdb_manager import init_duckdb, safe_ident

    try:
        db = init_duckdb(user_id=user_id)
        qname = safe_ident(ds['table_name'])
        cols = db.execute(f"DESCRIBE {qname}").fetchall()
        stats = db.execute(f"SUMMARIZE {qname}").fetchall()
        sample_df = db.query_df(f"SELECT * FROM {qname} LIMIT 5")

        return JSONResponse({
            "name": name,
            "table_name": ds["table_name"],
            "source_type": ds["source_type"],
            "row_count": ds["row_count"],
            "columns": [{"name": c[0], "type": c[1]} for c in cols],
            "statistics": [
                {"column": s[0], "type": s[1], "min": str(s[2]) if s[2] is not None else None,
                 "max": str(s[3]) if s[3] is not None else None,
                 "avg": str(s[4]) if s[4] is not None else None,
                 "std": str(s[5]) if s[5] is not None else None,
                 "count": s[6], "null_count": s[7]}
                for s in stats
            ],
            "sample": sample_df.to_dict(orient="records"),
        })
    except Exception as e:
        # DuckDB 中表可能尚未加载，返回元数据中的 schema_json
        import json as _json
        return JSONResponse({
            "name": name,
            "table_name": ds["table_name"],
            "source_type": ds["source_type"],
            "row_count": ds["row_count"],
            "columns": _json.loads(ds.get("schema_json", "[]")),
            "note": "DuckDB 中未加载，显示的是缓存 schema",
        })


@app.post("/api/datasources/reload")
async def reload_datasources(request: Request):
    """热加载 datasources.yml 配置的数据库连接。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)

    try:
        from database.duckdb_manager import init_duckdb
    except ModuleNotFoundError:
        from agent.database.duckdb_manager import init_duckdb

    try:
        db = init_duckdb(user_id=user_id)
        result = db.register_external_databases()
        return JSONResponse({"success": True, **result})
    except Exception as e:
        logger.error(f"Datasource reload failed: {traceback.format_exc()}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ── 知识库管理（方案C-5） ──

def _kb_data_dir() -> str:
    try:
        from utils.config_handler import chroma_conf
    except ModuleNotFoundError:
        from agent.utils.config_handler import chroma_conf
    return get_abs_path(chroma_conf["data_path"])


def _kb_allowed_types() -> tuple:
    try:
        from utils.config_handler import chroma_conf
    except ModuleNotFoundError:
        from agent.utils.config_handler import chroma_conf
    return tuple(chroma_conf["allowed_knowledge_file_type"])


@app.get("/api/knowledge/files")
async def kb_list_files(request: Request):
    """列出 data/ 下知识库文件，含大小/类型/md5/是否已入库。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    data_dir = _kb_data_dir()
    allowed = _kb_allowed_types()
    try:
        from utils.file_handler import get_file_md5_hex
    except ModuleNotFoundError:
        from agent.utils.file_handler import get_file_md5_hex

    vs = _get_vector_store()
    ingested_md5 = vs._load_md5_store()
    files = []
    if os.path.isdir(data_dir):
        for fname in sorted(os.listdir(data_dir)):
            fpath = os.path.join(data_dir, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower().lstrip(".")
            if ext not in allowed:
                continue
            size = os.path.getsize(fpath)
            md5 = get_file_md5_hex(fpath) or ""
            files.append({
                "filename": fname,
                "size": size,
                "type": ext,
                "md5": md5,
                "ingested": md5 in ingested_md5 if md5 else False,
            })
    return JSONResponse({"files": files, "count": len(files)})


@app.post("/api/knowledge/upload")
async def kb_upload(request: Request, files: list[UploadFile] = File(...)):
    """上传文件到 data/ 并增量入库。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    data_dir = _kb_data_dir()
    allowed = _kb_allowed_types()
    os.makedirs(data_dir, exist_ok=True)
    vs = _get_vector_store()

    results = []
    for f in files:
        fname = os.path.basename(f.filename or "")
        ext = os.path.splitext(fname)[1].lower().lstrip(".")
        if ext not in allowed:
            results.append({"filename": fname, "success": False,
                            "error": f"不支持的文件类型: {ext}"})
            continue
        fpath = os.path.join(data_dir, fname)
        try:
            content = await f.read()
            with open(fpath, "wb") as out:
                out.write(content)
            chunks, skipped = vs.load_single_document(fpath)
            results.append({
                "filename": fname,
                "success": True,
                "chunks": chunks,
                "skipped": skipped,
            })
        except Exception as e:
            logger.error(f"知识库上传入库失败 {fname}: {traceback.format_exc()}")
            results.append({"filename": fname, "success": False, "error": str(e)})
    return JSONResponse({"results": results})


@app.delete("/api/knowledge/files/{filename}")
async def kb_delete_file(request: Request, filename: str):
    """删除 data/ 下指定文件，并从向量库移除其分片。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    # 防路径穿越
    if "/" in filename or "\\" in filename or filename in ("", ".", ".."):
        return JSONResponse({"error": "非法文件名"}, status_code=400)
    data_dir = _kb_data_dir()
    fpath = os.path.join(data_dir, filename)
    if not os.path.isfile(fpath):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    try:
        vs = _get_vector_store()
        removed = vs.delete_by_source(fpath)
        os.remove(fpath)
        return JSONResponse({"success": True, "removed_chunks": removed})
    except Exception as e:
        logger.error(f"知识库删除失败 {filename}: {traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/knowledge/reindex")
async def kb_reindex(request: Request):
    """清空向量库并全量重建索引（二次确认通过 confirm=true 才执行）。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("confirm"):
        return JSONResponse({"error": "需传 confirm=true 以确认全量重建"}, status_code=400)
    try:
        vs = _get_vector_store()
        result = vs.reindex_all()
        return JSONResponse({"success": True, **result})
    except Exception as e:
        logger.error(f"知识库重建失败: {traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/knowledge/stats")
async def kb_stats(request: Request):
    """返回知识库统计信息。"""
    user_id = await _get_user_id(request)
    if user_id == "anonymous":
        return JSONResponse({"error": "未登录"}, status_code=401)
    try:
        vs = _get_vector_store()
        return JSONResponse(vs.get_stats())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── 静态文件（报告和图表） ──
_reports_dir = get_abs_path("reports")
if os.path.exists(_reports_dir):
    app.mount("/reports", StaticFiles(directory=_reports_dir), name="reports")


def _sanitize_result(result: dict) -> dict:
    """确保结果可 JSON 序列化。"""
    sanitized = {}
    for key, value in result.items():
        if key == "results":
            sanitized[key] = _sanitize_dict(value)
        elif isinstance(value, dict):
            sanitized[key] = _sanitize_dict(value)
        elif isinstance(value, list):
            sanitized[key] = [_sanitize_dict(v) if isinstance(v, dict) else v for v in value]
        else:
            sanitized[key] = value
    return sanitized


def _sanitize_dict(d: dict) -> dict:
    """递归清理字典中的非 JSON 类型。"""
    if not isinstance(d, dict):
        return d
    clean = {}
    for k, v in d.items():
        if isinstance(v, dict):
            clean[k] = _sanitize_dict(v)
        elif isinstance(v, list):
            clean[k] = [_sanitize_dict(i) if isinstance(i, dict) else i for i in v]
        elif isinstance(v, (str, int, float, bool, type(None))):
            clean[k] = v
        else:
            clean[k] = str(v)
    return clean


def _to_web_path(abs_path: str) -> str:
    """将绝对路径转为 Web 可访问的相对路径。
    D:\\...\\reports\\charts\\foo.html → /reports/charts/foo.html
    """
    import re
    # 标准化路径分隔符
    normalized = abs_path.replace("\\", "/")
    # 提取 reports/ 之后的部分
    match = re.search(r"/reports/(.+)", normalized)
    if match:
        return f"/reports/{match.group(1)}"
    # 如果路径已经以 / 开头且存在，直接返回
    if normalized.startswith("/reports/"):
        return normalized
    # 无法转换，返回原名
    return normalized


def _normalize_paths(obj):
    """递归转换 dict/list 中的绝对路径为 Web URL。"""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            # 转换 path/url 字段
            if k in ("path", "file_path") and isinstance(v, str) and (":\\" in v or ":/" in v):
                result[k] = v  # 保留原始路径
                result["url"] = _to_web_path(v)  # 添加 Web 可访问 URL
            elif k == "charts" and isinstance(v, list):
                result[k] = [
                    {**c, "url": _to_web_path(c.get("path", ""))}
                    if isinstance(c, dict) and c.get("path") else c
                    for c in v
                ]
            elif isinstance(v, (dict, list)):
                result[k] = _normalize_paths(v)
            else:
                result[k] = v
        return result
    elif isinstance(obj, list):
        return [_normalize_paths(item) if isinstance(item, (dict, list)) else item for item in obj]
    return obj


def start_server(host: str = "0.0.0.0", port: int = 8502):
    """启动 FastAPI 服务。"""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    start_server()
