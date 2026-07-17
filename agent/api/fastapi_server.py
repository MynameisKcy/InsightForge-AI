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
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #f5f7fa; height: 100vh; display: flex; overflow: hidden; }

/* ── 侧边栏 ── */
.sidebar { width: 280px; min-width: 280px; height: 100vh; background: #1a1a2e;
           display: flex; flex-direction: column; border-right: 1px solid #2d3748; }
.sidebar-header { padding: 16px; border-bottom: 1px solid #2d3748; }
.sidebar-header h1 { font-size: 16px; color: #fff; margin-bottom: 12px; }
.sidebar-header .user-info { font-size: 12px; color: #a0aec0; margin-bottom: 8px; }
.btn-new-session { width: 100%; padding: 10px; background: #e94560; color: #fff;
                    border: none; border-radius: 8px; font-size: 14px; cursor: pointer;
                    transition: background .2s; }
.btn-new-session:hover { background: #c23152; }
.session-list { flex: 1; overflow-y: auto; padding: 8px 0; }
.session-item { padding: 12px 16px; cursor: pointer; transition: background .15s;
                border-left: 3px solid transparent; }
.session-item:hover { background: #16213e; }
.session-item.active { background: #16213e; border-left-color: #e94560; }
.session-item .s-title { color: #e2e8f0; font-size: 13px; font-weight: 500;
                         white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                         margin-bottom: 4px; }
.session-item .s-time { color: #718096; font-size: 11px; }
.sidebar-footer { padding: 12px 16px; border-top: 1px solid #2d3748; }
.btn-logout { width: 100%; padding: 8px; background: transparent; color: #a0aec0;
              border: 1px solid #4a5568; border-radius: 6px; font-size: 13px;
              cursor: pointer; transition: all .2s; }
.btn-logout:hover { color: #e94560; border-color: #e94560; }
.no-sessions { padding: 20px 16px; color: #718096; font-size: 13px; text-align: center; }

/* ── 主内容区 ── */
.main-area { flex: 1; display: flex; flex-direction: column; height: 100vh; }
.chat-container { flex: 1; overflow-y: auto; padding: 20px 24px; display: flex;
                  flex-direction: column; gap: 16px; max-width: 900px;
                  margin: 0 auto; width: 100%; }
.message { display: flex; gap: 12px; max-width: 85%; animation: fadeIn .3s; }
.message.user { align-self: flex-end; flex-direction: row-reverse; }
.message.assistant { align-self: flex-start; }
.avatar { width: 36px; height: 36px; border-radius: 50%; display: flex;
          align-items: center; justify-content: center; font-size: 18px;
          flex-shrink: 0; }
.message.user .avatar { background: #e94560; }
.message.assistant .avatar { background: #0f3460; }
.bubble { padding: 12px 16px; border-radius: 14px; line-height: 1.6; font-size: 14px;
          word-break: break-word; }
.message.user .bubble { background: #e94560; color: #fff;
                         border-bottom-right-radius: 4px; }
.message.assistant .bubble { background: #fff; color: #2d3748;
                              border-bottom-left-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
.bubble h1,.bubble h2,.bubble h3 { margin: 8px 0 4px; font-size: 15px; }
.bubble table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 12px; }
.bubble th,.bubble td { border: 1px solid #e2e8f0; padding: 6px 8px; text-align: left; }
.bubble th { background: #edf2f7; }
.bubble ul,.bubble ol { padding-left: 20px; margin: 4px 0; }
.bubble code { background: #edf2f7; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
.bubble pre { background: #1a202c; color: #e2e8f0; padding: 12px; border-radius: 8px;
              overflow-x: auto; font-size: 12px; margin: 8px 0; }
.bubble blockquote { border-left: 3px solid #e94560; padding-left: 12px;
                     color: #718096; margin: 8px 0; }
.bubble hr { border: none; border-top: 1px solid #e2e8f0; margin: 12px 0; }
.bubble img { max-width: 100%; border-radius: 8px; }
.input-area { padding: 16px 24px; background: #fff; border-top: 1px solid #e2e8f0; }
.input-row { display: flex; gap: 12px; max-width: 900px; margin: 0 auto; }
.input-row input { flex: 1; padding: 12px 16px; border: 2px solid #e2e8f0;
                    border-radius: 12px; font-size: 14px; outline: none;
                    transition: border-color .2s; }
.input-row input:focus { border-color: #e94560; }
.input-row button { padding: 12px 24px; background: #e94560; color: #fff;
                    border: none; border-radius: 12px; font-size: 14px; font-weight: 600;
                    cursor: pointer; transition: background .2s; }
.input-row button:hover { background: #c23152; }
.input-row button:disabled { opacity: .6; cursor: not-allowed; }
.typing-indicator { display: flex; gap: 4px; padding: 8px 0; }
.typing-indicator span { width: 8px; height: 8px; background: #a0aec0; border-radius: 50%;
                         animation: bounce 1.2s infinite; }
.typing-indicator span:nth-child(2) { animation-delay: .2s; }
.typing-indicator span:nth-child(3) { animation-delay: .4s; }
@keyframes bounce { 0%,60%,100% { transform: translateY(0); } 30% { transform: translateY(-8px); } }
.chat-status { display: flex; align-items: center; gap: 8px; padding: 4px 0;
               color: #718096; font-size: 12px; font-style: italic; }
.chat-status .spinner { width: 14px; height: 14px; border: 2px solid #e2e8f0;
                        border-top: 2px solid #e94560; border-radius: 50%;
                        animation: spin .7s linear infinite; flex-shrink: 0; }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes fadeIn { from { opacity: 0; transform: translateY(8px); }
                    to { opacity: 1; transform: translateY(0); } }
.welcome-msg { text-align: center; padding: 60px 20px; color: #a0aec0; }
.welcome-msg h2 { font-size: 20px; color: #4a5568; margin-bottom: 8px; }
.welcome-msg p { font-size: 14px; line-height: 1.8; }

/* ── 知识库管理 ── */
.kb-section { border-top: 1px solid #2d3748; padding: 12px 0 0; }
.kb-header { padding: 0 16px 8px; display: flex; justify-content: space-between;
             align-items: center; }
.kb-header h2 { font-size: 13px; color: #e2e8f0; font-weight: 600; }
.kb-stats { font-size: 11px; color: #718096; }
.kb-body { padding: 0 12px 8px; max-height: 200px; overflow-y: auto; }
.kb-file { display: flex; align-items: center; gap: 6px; padding: 6px 8px;
           border-radius: 6px; font-size: 12px; color: #cbd5e0;
           transition: background .15s; }
.kb-file:hover { background: #16213e; }
.kb-file .kb-name { flex: 1; white-space: nowrap; overflow: hidden;
                   text-overflow: ellipsis; }
.kb-file .kb-badge { font-size: 10px; padding: 1px 6px; border-radius: 8px; flex-shrink: 0; }
.kb-badge.in { background: #2f855a; color: #fff; }
.kb-badge.out { background: #4a5568; color: #cbd5e0; }
.kb-del { background: transparent; border: none; color: #718096; cursor: pointer;
          font-size: 14px; padding: 0 2px; flex-shrink: 0; }
.kb-del:hover { color: #e94560; }
.kb-upload { padding: 0 16px 8px; }
.kb-upload input[type=file] { display: none; }
.kb-btn { width: 100%; padding: 7px; font-size: 12px; border-radius: 6px;
          border: 1px dashed #4a5568; background: transparent; color: #a0aec0;
          cursor: pointer; transition: all .15s; }
.kb-btn:hover { color: #e94560; border-color: #e94560; }
.kb-reindex { padding: 0 16px 12px; }
.kb-reindex .kb-btn { border-style: solid; font-size: 11px; }

/* ── 响应式 ── */
@media (max-width: 700px) {
  .sidebar { width: 60px; min-width: 60px; }
  .sidebar-header h1, .sidebar-header .user-info,
  .session-item .s-title, .session-item .s-time,
  .btn-new-session span, .btn-logout span { display: none; }
  .sidebar-header { padding: 10px; }
  .session-item { padding: 10px; text-align: center; }
  .btn-new-session { padding: 10px; font-size: 16px; }
  .btn-new-session::after { content: '+'; }
}
</style>
</head>
<body>

<!-- ── 侧边栏 ── -->
<div class="sidebar">
  <div class="sidebar-header">
    <h1>🤖 AI Data Analyst</h1>
    <div class="user-info" id="userDisplay"></div>
    <button class="btn-new-session" onclick="newSession()"><span>+ 新会话</span></button>
  </div>
  <div class="session-list" id="sessionList">
    <div class="no-sessions">暂无会话记录</div>
  </div>
  <!-- ── 知识库管理（方案C） ── -->
  <div class="kb-section">
    <div class="kb-header">
      <h2>📚 知识库</h2>
      <span class="kb-stats" id="kbStats">-</span>
    </div>
    <div class="kb-body" id="kbFileList">
      <div class="kb-file" style="color:#718096;justify-content:center;">加载中...</div>
    </div>
    <div class="kb-upload">
      <input type="file" id="kbFileInput" multiple accept=".txt,.pdf,.docx,.md">
      <button class="kb-btn" onclick="document.getElementById('kbFileInput').click()">＋ 上传并入库</button>
    </div>
    <div class="kb-reindex">
      <button class="kb-btn" onclick="kbReindex()">⟳ 全量重建索引</button>
    </div>
  </div>
  <div class="sidebar-footer">
    <button class="btn-logout" onclick="logout()"><span>登出</span></button>
  </div>
</div>

<!-- ── 主内容区 ── -->
<div class="main-area">
  <div class="chat-container" id="chatContainer">
    <div class="welcome-msg">
      <h2>👋 你好，我是 AI 数据分析顾问</h2>
      <p>我可以帮你分析销售趋势、产品表现、利润变化，生成图表和报告。<br>
      也可以回答知识库问题、查询外部数据。<br><br>
      请直接描述你的需求，我会自动选择合适的分析方式。</p>
    </div>
  </div>
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
  const msgDiv = bubble.parentElement;
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
        if (chartUrl) {
          const iframe = document.createElement('iframe');
          iframe.src = chartUrl.startsWith('/') ? chartUrl : '/' + chartUrl.replace(/\\\\/g, '/');
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
  const container = document.getElementById('chatContainer');
  const div = document.createElement('div');
  div.className = `message ${role}`;
  const statusDiv = role === 'assistant'
    ? '<div class="chat-status" style="display:none"><span class="spinner"></span><span class="status-text"></span></div>'
    : '';
  div.innerHTML = `
    <div class="avatar">${role === 'user' ? '👤' : '🤖'}</div>
    <div class="bubble">${escapeHtml(text)}</div>${statusDiv}`;
  container.appendChild(div);
  return div;
}

function renderMarkdown(text) {
  let html = text;
  // 代码块
  html = html.replace(/```(\\w*)\\n([\\s\\S]*?)```/g, (_, lang, code) =>
    `<pre><code>${escapeHtml(code.trim())}</code></pre>`);
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
  // 图片
  html = html.replace(/!\\[(.*?)\\]\\((.*?)\\)/g, '<img src="$2" alt="$1">');
  // 链接
  html = html.replace(/\\[(.*?)\\]\\((.*?)\\)/g, '<a href="$2">$1</a>');
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

function scrollToBottom() {
  const container = document.getElementById('chatContainer');
  setTimeout(() => { container.scrollTop = container.scrollHeight; }, 50);
}

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
    alert('上传完成：成功 ' + ok + ' / ' + files.length + ' 个文件');
    loadKbFiles();
  } catch(err) { alert('上传失败: ' + err.message); }
  e.target.value = '';
});

async function deleteKbFile(filename) {
  if (!confirm('确认删除知识库文件及其分片？\n' + filename)) return;
  try {
    const r = await fetch('/api/knowledge/files/' + encodeURIComponent(filename), {
      method: 'DELETE', headers: authHeaders()
    });
    const data = await r.json();
    if (data.success !== undefined && !data.success) {
      alert(data.error || '删除失败'); return;
    }
    loadKbFiles();
  } catch(e) { alert('删除失败: ' + e.message); }
}

async function kbReindex() {
  if (!confirm('全量重建将清空当前向量库并重新入库所有文件，耗时较长。确认继续？')) return;
  try {
    const r = await fetch('/api/knowledge/reindex', {
      method: 'POST', headers: authHeaders(),
      body: JSON.stringify({confirm: true})
    });
    const data = await r.json();
    if (data.error) { alert(data.error); return; }
    alert('重建完成：重载 ' + (data.reloaded_files || 0) + ' 个文件，共 ' +
          ((data.stats && data.stats.total_chunks) || 0) + ' 个分片');
    loadKbFiles();
  } catch(e) { alert('重建失败: ' + e.message); }
}

// ── 初始化 ──
loadSessions();
loadKbFiles();
document.getElementById('userInput').focus();
</script>
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
    turns = _long_term_memory.get_last_n_turns(user_id, n=limit)
    return JSONResponse(content={"user_id": user_id, "turns": turns, "count": len(turns)})


@app.get("/api/sessions")
async def api_list_sessions(request: Request):
    """获取用户的所有会话列表（按最近活跃排序）。"""
    user_id = await _get_user_id(request)
    sessions = _long_term_memory.get_user_sessions(user_id)
    return JSONResponse(content={"user_id": user_id, "sessions": sessions, "count": len(sessions)})


@app.get("/api/sessions/{session_id}")
async def api_get_session(request: Request, session_id: str):
    """获取指定会话的完整对话历史。"""
    user_id = await _get_user_id(request)
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
