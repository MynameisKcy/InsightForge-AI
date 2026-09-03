
let isProcessing = false;
let currentController = null;   // 当前 SSE 请求的 AbortController，供停止按钮中止
let userStopped = false;       // 区分"用户主动停止" vs "真超时"
let authToken = Auth.getToken();
let accountName = '';
let currentSessionId = '';

// ── SSE 协议词汇（与 utils/sse_protocol.py 锁步，tests/test_sse_protocol.py 校验）──
// 服务端是发射方；本表是前端消费侧唯一词表，业务代码禁止散写 '[XXX]' 字面量。
var SSE_PROTOCOL = {
  THINKING:        '[THINKING]',
  SESSION:         '[SESSION]',
  SESSIONS_RELOAD: '[SESSIONS_RELOAD]',
  TRACE:           '[TRACE]',
  STEP:            '[STEP]',
  STEP_TIMING:     '[STEP_TIMING]',
  KEEPALIVE:       '[KEEPALIVE]',
  DONE:            '[DONE]',
  ERROR:           '[ERROR]',
  CHART:           '[CHART]',
  METRICS:         '[METRICS]',
  DECISION:        '[DECISION]'
};
// 线上词汇 '[TOKEN]' → 裸名 'TOKEN'（processLine 判定用）
var SSE_TOKENS = {};
(function () { for (var k in SSE_PROTOCOL) SSE_TOKENS[SSE_PROTOCOL[k].slice(1, -1)] = k; })();

// 统一帧解析：'[TOKEN]payload' 或 '[TOKEN:payload]' → {token, payload}；
// 非 token 行返回 null（调用方回落正文）。语义与 Python 侧 parse_frame 一致：
// 包裹式（STEP/METRICS/DECISION/CHART）行尾最后一个 ']' 是终结符，
// payload 内部可含 ']'；裸式 payload 为 ']' 后的全部文本。
function parseSSEFrame(data) {
  var m = /^\[([A-Z_]+)/.exec(data);
  if (!m) return null;
  var token = m[1], rest = data.slice(m[0].length);
  if (rest.charAt(0) === ':') {
    var p = rest.slice(1);
    if (p.charAt(p.length - 1) === ']') p = p.slice(0, -1);
    return {token: token, payload: p};
  }
  if (rest.charAt(0) === ']') return {token: token, payload: rest.slice(1)};
  return null;
}

if (!authToken) { window.location.href = '/'; }

document.getElementById('userDisplay').innerHTML = (window.Icons ? window.Icons.user : '👤') +
    '<span class="uname">' + escapeHtml(accountName) + '</span>';
// 加载个人信息（昵称/头像），优先显示昵称，回退到账号
loadProfile();

async function loadProfile() {
  try {
    var r = await Auth.authedFetch('/api/profile', {headers: authHeaders()});
    if (!r.ok) return;
    var d = await r.json();
    var display = document.getElementById('userDisplay');
    var name = d.nickname || accountName || '未登录';
    display.innerHTML = (window.Icons ? window.Icons.user : '👤') +
                        '<span class="uname">' + escapeHtml(name) + '</span>';
  } catch(e) { /* 忽略，保持账号显示 */ }
}

// ── 个人信息弹窗（昵称 / 头像 / 密码） ──
function openProfileModal() {
  var ov = document.getElementById('profileOverlay');
  ov.classList.add('show');
  // 填充当前值
  Auth.authedFetch('/api/profile', {headers: authHeaders()}).then(function(r){return r.json();}).then(function(d){
    if (!d) return;
    document.getElementById('pfNickname').value = d.nickname || '';
    document.getElementById('pfAccount').value = d.account || '';
  });
  // 清空密码字段
  document.getElementById('pfOldPwd').value = '';
  document.getElementById('pfNewPwd').value = '';
  document.getElementById('pfNewPwd2').value = '';
}

function closeProfileModal() {
  document.getElementById('profileOverlay').classList.remove('show');
}

async function saveProfile() {
  var nickname = document.getElementById('pfNickname').value.trim();
  var newPwd = document.getElementById('pfNewPwd').value;
  var newPwd2 = document.getElementById('pfNewPwd2').value;
  var ok = true;
  // 1. 昵称
  try {
    await Auth.authedFetch('/api/profile', {
      method: 'POST',
      headers: Object.assign({'Content-Type':'application/json'}, authHeaders()),
      body: JSON.stringify({nickname: nickname})
    });
    await loadProfile();
  } catch(e) { showToast('昵称保存失败', 'error'); ok = false; }
  // 2. 密码（仅在填写了新密码时才改）
  if (newPwd) {
    if (newPwd.length < 8) { showToast('新密码至少 8 位', 'error'); return; }
    if (newPwd !== newPwd2) { showToast('两次新密码不一致', 'error'); return; }
    try {
      var r = await Auth.authedFetch('/api/password', {
        method: 'POST',
        headers: Object.assign({'Content-Type':'application/json'}, authHeaders()),
        body: JSON.stringify({
          old_password: document.getElementById('pfOldPwd').value,
          new_password: newPwd
        })
      });
      var d = await r.json();
      if (!d.success) { showToast('改密失败：' + (d.error || '未知错误'), 'error'); ok = false; }
      else { showToast('密码已更新', 'success'); }
    } catch(e) { showToast('改密失败: ' + e, 'error'); ok = false; }
  }
  if (ok) { showToast('已保存', 'success'); closeProfileModal(); }
}

// ── 可折叠侧边栏分区 ──
function toggleSection(name) {
  var suffix = {ds:'Ds', kb:'Kb', set:'Set', metrics:'Metrics'}[name];
  if (!suffix) return;
  var body = document.getElementById('sectionBody' + suffix);
  var chevron = document.getElementById('chevron' + suffix);
  if (!body || !chevron) return;
  body.classList.toggle('collapsed');
  chevron.classList.toggle('collapsed');
  // 账号设置面板展开时加载掩码配置
  if (name === 'set' && !body.classList.contains('collapsed')) {
    loadSettings();
  }
}
// 强制展开某分区（suffix 如 'Ds'/'Files'），上传后让用户立刻看到结果
function _expandSection(suffix) {
  var body = document.getElementById('sectionBody' + suffix);
  var chevron = document.getElementById('chevron' + suffix);
  if (body) body.classList.remove('collapsed');
  if (chevron) chevron.classList.remove('collapsed');
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
    const r = await Auth.authedFetch('/api/sessions', {
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
    const sid = s.session_id;
    const title = escapeHtml(s.title || '未命名会话');
    return `<div class="session-item${active}" data-sid="${sid}">
      <div class="s-main" onclick="switchSession('${sid}')">
        <div class="s-title" data-sid="${sid}">${title}</div>
        <div class="s-time">${timeStr}</div>
      </div>
      <div class="s-actions">
        <button class="s-act" title="重命名" onclick="renameSession('${sid}', event)">✎</button>
        <button class="s-act" title="删除" onclick="deleteSession('${sid}', event)">🗑</button>
      </div>
    </div>`;
  }).join('');
}

async function deleteSession(sid, ev) {
  if (ev) { ev.stopPropagation(); }
  if (!confirm('确定删除该会话？删除后不可恢复。')) return;
  try {
    const r = await Auth.authedFetch('/api/sessions/' + sid, {
      method: 'DELETE',
      headers: {'Authorization': 'Bearer ' + authToken}
    });
    if (r.ok) {
      if (sid === currentSessionId) {
        currentSessionId = '';
        document.getElementById('chatContainer').innerHTML =
          '<div class="welcome-msg"><p>开始新的对话吧</p></div>';
      }
      await loadSessions();
    } else {
      alert('删除失败');
    }
  } catch(e) { alert('删除失败: ' + e.message); }
}

function renameSession(sid, ev) {
  if (ev) { ev.stopPropagation(); }
  const item = document.querySelector(`.session-item[data-sid="${sid}"]`);
  if (!item) return;
  const titleEl = item.querySelector('.s-title');
  if (!titleEl || titleEl.querySelector('input')) return;
  const oldTitle = titleEl.textContent.trim();
  const input = document.createElement('input');
  input.type = 'text';
  input.value = oldTitle;
  input.className = 's-rename-input';
  input.maxLength = 60;
  titleEl.textContent = '';
  titleEl.appendChild(input);
  input.focus();
  input.select();
  let done = false;
  const commit = async () => {
    if (done) return; done = true;
    const newTitle = input.value.trim() || oldTitle;
    titleEl.textContent = newTitle;
    if (newTitle && newTitle !== oldTitle) {
      try {
        await Auth.authedFetch('/api/sessions/' + sid, {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + authToken},
          body: JSON.stringify({title: newTitle})
        });
        await loadSessions();
      } catch(e) { /* 忽略，标题本地已更新 */ }
    }
  };
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    else if (e.key === 'Escape') { done = true; titleEl.textContent = oldTitle; }
    e.stopPropagation();
  });
  input.addEventListener('click', (e) => e.stopPropagation());
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
    const r = await Auth.authedFetch('/api/sessions/' + sessionId, {
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
          const div = appendMessage(role, m.content || '');
          // 历史会话消息需要渲染 markdown（与实时流式一致），并给报告类消息追加导出按钮
          if (role === 'assistant') {
            const bubble = div.querySelector('.bubble');
            const rawText = m.content || '';
            if (bubble) {
              // 提取图表标记 [CHART:url]，剥离后渲染 markdown，再追加图表 iframe
              var chartInfo = _extractCharts(rawText);
              bubble.innerHTML = renderMarkdown(chartInfo.text);
              _syncRaw(bubble, chartInfo.text);
              _renderChartIframes(bubble, chartInfo.chartUrls);
              // 报告类消息：追加导出按钮
              if (chartInfo.text.length > 50 && /^#{1,3}\s/m.test(chartInfo.text)) {
                appendExportBar(bubble, chartInfo.text);
              }
            }
          }
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
  // 带上当前会话 id：后端登出时同步清理该会话的 token 统计（plan §4.2⑥）
  Auth.authedFetch('/api/logout', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + authToken},
    body: JSON.stringify({session_id: currentSessionId})
  });
  Auth.clearToken();
  sessionStorage.removeItem('account');
  window.location.href = '/';
}

function authHeaders() {
  return {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + authToken};
}

async function sendMessage() {
  if (isProcessing) { showToast('请等待当前回复完成', 'info', 2000); return; }
  const input = document.getElementById('userInput');
  const text = input.value.trim();
  if (!text) return;

  isProcessing = true;
  userStopped = false;
  input.value = '';
  setSendButtonState(true);   // 切换为"停止"按钮

  // 移除欢迎消息
  const welcome = document.querySelector('.welcome-msg');
  if (welcome) welcome.remove();

  appendMessage('user', text);

  const assistantMsg = appendMessage('assistant', '');
  const bubble = assistantMsg.querySelector('.bubble');
  // 分区结构:step-progress / content / charts / export-bar 独立 div,互不销毁
  // (commit 1 stage_timing 修复:原 L721 renderMarkdown 重写整 bubble 销毁 step 清单)
  bubble.innerHTML =
    '<div class="stream-step-progress"></div>' +
    '<div class="stream-content"><div class="typing-indicator"><span></span><span></span><span></span></div></div>' +
    '<div class="stream-charts"></div>' +
    '<div class="stream-export-bar"></div>';
  scrollToBottom();

  try {
    await streamChat(text, bubble);
  } catch (err) {
    // 用户主动停止或超时：streamChat 内部已处理气泡，不在此覆写
    if (!userStopped) {
      bubble.innerHTML = `<span style="color:var(--color-error)">请求失败: ${escapeHtml(err.message)}</span>`;
    }
  } finally {
    isProcessing = false;
    currentController = null;
    setSendButtonState(false);  // 切回"发送"按钮
    document.getElementById('userInput').focus();
  }
}

// 发送/停止按钮同位置切换
function setSendButtonState(processing) {
  const btn = document.getElementById('sendBtn');
  if (!btn) return;
  if (processing) {
    btn.textContent = '⏹ 停止';
    btn.classList.add('stop-mode');
    btn.onclick = stopGeneration;
  } else {
    btn.textContent = '发送';
    btn.classList.remove('stop-mode');
    btn.onclick = sendMessage;
    btn.disabled = false;
  }
}

// 用户主动停止当前生成（保留已生成内容）
function stopGeneration() {
  userStopped = true;
  if (currentController) {
    try { currentController.abort(); } catch(e) {}
  }
}

async function streamChat(text, bubble) {
  const body = { query: text };
  if (currentSessionId) body.session_id = currentSessionId;

  // ── 超时兜底：避免后端长时间不回包时 isProcessing/sendBtn 永久卡死 ──
  const controller = new AbortController();
  let idleTimer;
  const resetIdle = () => {
    clearTimeout(idleTimer);
    // 收到任意数据即证明流活着，重置计时；300s 内无新数据则中止
    // （分析+绘图等多步 LLM 流程可能数分钟，配合后端心跳保活）
    idleTimer = setTimeout(() => controller.abort(), 300000);
  };
  resetIdle();
  currentController = controller;   // 暴露给停止按钮中止

  let response;
  try {
    response = await Auth.authedFetch('/api/chat', {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (err) {
    clearTimeout(idleTimer);
    if (err.name === 'AbortError') throw new Error('请求超时，请重试');
    throw err;
  }

  if (response.status === 401) {
    // authedFetch 内部已清 token 并（在主应用上）跳转回落地页，且去重——只触发一次。
    // 这里只负责给用户一次可见提示，不再重复 setTimeout 跳转，避免多次通知。
    clearTimeout(idleTimer);
    if (!window._authExpiredNotified) {
      window._authExpiredNotified = true;
      showToast('登录已失效，请重新登录', 'error', 3000);
    }
    throw new Error('未登录，请重新登录');
  }
  if (!response.ok) {
    clearTimeout(idleTimer);
    throw new Error(`HTTP ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let fullText = '';
  let errorRendered = false; // [ERROR] 已渲染则不覆盖为"空响应"
  let thinking = true; // 默认为思考状态
  let statusEl = null; // 思考状态 DOM 元素

  // ── 执行用时计时器：首个 [STEP] 起表，[DONE]/[ERROR] 停表（Issue ⑤）──
  let stepStartedAt = null;
  let stepTimerId = null;

  function fmtElapsed(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    var m = Math.floor(s / 60);
    return m > 0 ? m + '分' + (s % 60) + '秒' : s + '秒';
  }

  function ensureStepTimer() {
    if (stepTimerId || !statusEl) return;
    stepStartedAt = Date.now();
    var t = statusEl.querySelector('.status-timer');
    if (!t) {
      t = document.createElement('span');
      t.className = 'status-timer';
      statusEl.appendChild(t);
    }
    stepTimerId = setInterval(function () {
      t.textContent = ' · ' + fmtElapsed(Date.now() - stepStartedAt);
    }, 1000);
  }

  function stopStepTimer(finalText) {
    if (!stepTimerId) return;
    clearInterval(stepTimerId);
    stepTimerId = null;
    if (finalText && statusEl) {
      var st = statusEl.querySelector('.status-text');
      var t = statusEl.querySelector('.status-timer');
      if (st) st.textContent = finalText;
      if (t) t.textContent = ' · 用时 ' + fmtElapsed(Date.now() - (stepStartedAt || Date.now()));
    }
  }

  // 查找当前消息的 status 行
  const msgDiv = bubble.closest('.message');
  if (msgDiv) {
    statusEl = msgDiv.querySelector('.chat-status');
    if (statusEl) {
      statusEl.style.display = 'flex';
      statusEl.querySelector('.status-text').textContent = 'AI 正在思考...';
    }
  }

  // ── Token/成本看板（SSE [METRICS] 事件驱动，面板缺失时静默）──
  function updateMetricsPanel(m) {
    try {
      document.getElementById('tokenInput').textContent = (m.input_tokens || 0).toLocaleString();
      document.getElementById('tokenOutput').textContent = (m.output_tokens || 0).toLocaleString();
      document.getElementById('tokenCalls').textContent = m.calls || 0;
      document.getElementById('tokenCost').textContent = (Number(m.cost_cny) || 0).toFixed(4);
    } catch (e) { /* 面板不存在 */ }
  }

  function resetMetricsPanel() {
    updateMetricsPanel({});
  }

  // ── 决策卡片（SSE [DECISION] 事件驱动；LLM 输出一律 escapeHtml 防 XSS）──
  function renderDecisionCard(bubble, d) {
    try {
      var body = '';
      if (d.reasoning) body += '<div class="decision-reasoning">' + escapeHtml(d.reasoning) + '</div>';
      if (d.tool) body += '<div class="decision-tool">调用工具 <code>' + escapeHtml(String(d.tool)) + '</code></div>';
      if (d.args && Object.keys(d.args).length) {
        // 内部参数默认收起（Issue ②）：原始字段不再平铺在对话页，点开才可见
        body += '<details class="decision-details"><summary>参数</summary>' +
                '<div class="decision-args"><code>' +
                escapeHtml(JSON.stringify(d.args)) + '</code></div></details>';
      }
      if (d.result_summary) body += '<div class="decision-result">' + escapeHtml(d.result_summary) + '</div>';
      if (!body) return;   // 空决策不渲染

      var head = '<div class="decision-header"><span class="decision-icon">' +
                 (d.source === 'planner' ? '🧭' : (d.tool ? '🛠' : '💭')) + '</span>' +
                 '<span class="decision-label">' +
                 (d.source === 'planner' ? '规划理由' : (d.tool ? '工具调用' : 'LLM 思考')) + '</span>';
      if (d.timing_ms != null) head += '<span class="decision-timing">' + d.timing_ms + ' ms</span>';
      head += '</div>';

      var card = document.createElement('div');
      card.className = 'decision-card' + (d.error ? ' decision-card--error' : '');
      card.innerHTML = head + body;
      bubble.appendChild(card);
    } catch (e) { /* 渐进增强 */ }
  }

  // ── 单行 SSE 事件处理（抽成闭包，供跨 chunk 行缓冲复用）──
  // 返回 true 表示遇到 [ERROR]，应终止整条流

  // 流分区: bubble 内部 4 个独立 div，markdown 重写只影响 stream-content，
  // 不销毁 step-progress / charts / export-bar（commit 1 修复）
  function streamPart(name) {
    var el = bubble.querySelector('.' + name);
    if (!el) { el = document.createElement('div'); el.className = name; bubble.appendChild(el); }
    return el;
  }
  function streamContent()       { return streamPart('stream-content'); }
  function streamStepProgress()  { return streamPart('stream-step-progress'); }
  function streamCharts()        { return streamPart('stream-charts'); }
  function streamExportBar()     { return streamPart('stream-export-bar'); }

  function processLine(line) {
    if (!line.startsWith('data: ')) return false;
    const data = line.slice(6);

    // 统一帧解析（词表见文件顶部 SSE_PROTOCOL）；未知 token / 非帧文本回落正文
    const frame = parseSSEFrame(data);
    const tok = frame ? frame.token : '';

    if (tok === 'TRACE') return false;   // Jaeger 链路诊断用，前端不渲染

    if (tok === 'DONE') {
      stopStepTimer('分析完成');
      // 报告类内容（含 markdown 标题或较长正文）流结束后追加导出按钮
      if (fullText && fullText.length > 50 && /^#{1,3}\s/m.test(fullText)) {
        appendExportBar(bubble, fullText);
      }
      return false;
    }

    if (tok === 'KEEPALIVE') return false;   // 心跳保活：仅 resetIdle，不渲染

    if (tok === 'STEP') {
      ensureStepTimer();   // 执行链路开始即起表（Issue ⑤）
      // 分析步骤进度：在 stream-step-progress 容器内渲染/更新步骤清单，
      // 并同步状态行(commit 1 修复:独立容器避免被 markdown 重写销毁)
      var stepData;
      try { stepData = JSON.parse(frame.payload.trim()); } catch (e) { return false; }
      if (statusEl) statusEl.style.display = 'flex';
      var prog = bubble.querySelector('.step-progress');
      if (stepData.type === 'plan') {
        // 步骤清单落到专属容器(由 streamChat 初始化时已建空 div)
        var sp = streamStepProgress();
        if (stepData.reasoning) {
          renderDecisionCard(sp, {source: 'planner', reasoning: stepData.reasoning});
        }
        prog = document.createElement('div');
        prog.className = 'step-progress';
        (stepData.steps || []).forEach(function (s) {
          var row = document.createElement('div');
          row.className = 'step pending';
          row.dataset.step = s.step;
          row.dataset.label = s.label || s.task || ('步骤 ' + s.step);
          row.innerHTML = '<span class="step-mark">○</span><span class="step-label">' +
                          escapeHtml(row.dataset.label) + '</span>';
          prog.appendChild(row);
        });
        sp.appendChild(prog);
        if (statusEl) statusEl.querySelector('.status-text').textContent =
          stepData.title || '正在执行分析流程...';
      } else if (prog) {
        var srow = prog.querySelector('.step[data-step="' + stepData.step + '"]');
        if (srow) {
          if (stepData.type === 'step_start') {
            srow.className = 'step active';
            srow.querySelector('.step-mark').innerHTML = '<span class="spinner"></span>';
            if (statusEl) statusEl.querySelector('.status-text').textContent = srow.dataset.label;
          } else if (stepData.type === 'step_done') {
            srow.className = 'step done';
            srow.querySelector('.step-mark').innerHTML = '✓';
          } else if (stepData.type === 'step_error') {
            srow.className = 'step error';
            srow.querySelector('.step-mark').innerHTML = '✗';
            if (statusEl) statusEl.querySelector('.status-text').textContent = (srow.dataset.label || '步骤') + ' 失败';
          }
        }
        if (stepData.type === 'status' && statusEl) {
          statusEl.querySelector('.status-text').textContent = stepData.text || '';
        }
      }
      scrollToBottom();
      return false;
    }

    if (tok === 'STEP_TIMING') {
      // 单阶段耗时:在对应 step row 旁追加 "X秒",前端可见的 per-step timing
      // (阶段 1 优化 ROI 测量点 + 用户感知改进)
      // 步骤清单在 stream-step-progress 容器内(commit 1 修复)
      var timingData;
      try { timingData = JSON.parse(frame.payload.trim()); } catch (e) { return false; }
      var step = timingData.step, ms = timingData.duration_ms;
      if (step == null || ms == null) return false;
      var prog = bubble.querySelector('.step-progress');
      if (!prog) return false;
      var srow = prog.querySelector('.step[data-step="' + step + '"]');
      if (!srow) return false;
      var dur = ms < 1000
        ? Math.round(ms) + 'ms'
        : (ms < 60000 ? (ms / 1000).toFixed(1) + '秒' : Math.floor(ms / 60000) + '分' + Math.round((ms % 60000) / 1000) + '秒');
      var labelEl = srow.querySelector('.step-label');
      if (labelEl) {
        // 避免重复追加(后端会同时发 step_done + step_timing 两次)
        var existing = labelEl.querySelector('.step-duration');
        if (existing) existing.textContent = ' · ' + dur;
        else {
          var span = document.createElement('span');
          span.className = 'step-duration';
          span.style.cssText = 'color:#888;font-size:0.85em;margin-left:4px;';
          span.textContent = ' · ' + dur;
          labelEl.appendChild(span);
        }
      }
      return false;
    }

    if (tok === 'ERROR') {
      stopStepTimer(null);   // 仅停表：状态行随后被隐藏，无终态文案可挂
      bubble.innerHTML = `<span style="color:var(--color-error)">${escapeHtml(frame.payload)}</span>`;
      errorRendered = true;
      if (statusEl) statusEl.style.display = 'none';
      return true;
    }

    if (tok === 'METRICS') {
      // Token/成本看板：值为该会话的服务端累计值，直接覆盖显示
      var m;
      try { m = JSON.parse(frame.payload.trim()); } catch (e) { return false; }
      updateMetricsPanel(m);
      return false;
    }

    if (tok === 'DECISION') {
      // Agent 决策卡片：LLM 推理 / 工具调用决策（渐进增强，渲染失败不影响对话流）
      var d;
      try { d = JSON.parse(frame.payload.trim()); } catch (e) { return false; }
      renderDecisionCard(bubble, d);
      scrollToBottom();
      return false;
    }

    if (tok === 'THINKING') {
      const status = frame.payload;
      if (statusEl) {
        statusEl.style.display = 'flex';
        statusEl.querySelector('.status-text').textContent = escapeHtml(status);
      }
      scrollToBottom();
      return false;
    }

    if (tok === 'SESSION') {
      var newSid = frame.payload;
      if (newSid !== currentSessionId) resetMetricsPanel();   // 换会话看板归零
      currentSessionId = newSid;
      updateActiveSession();
      return false;
    }

    if (tok === 'SESSIONS_RELOAD') {
      loadSessions();
      return false;
    }

    if (tok === 'CHART') {
      const chartUrl = frame.payload.trim();
      // XSS 防护：图表 URL 必须是站内相对路径（以 / 开头），拒绝 javascript:/外部 http
      if (chartUrl && chartUrl.charAt(0) === '/' && !chartUrl.startsWith('//')) {
        // 同一 chartUrl 已存在 → 跳过(commit 1 bug 修复:L703 if (!wrapper.dataset.created)
        // 永远 true,改为 if (wrapper.dataset.created) 仍不对——应按 url 去重)
        var chartsDiv = streamCharts();
        var existing = chartsDiv.querySelector('iframe[data-chart-url="' + cssEscape(chartUrl) + '"]');
        if (existing) return false;  // 已渲染过同一 url,跳过
        const iframe = document.createElement('iframe');
        iframe.src = chartUrl;
        iframe.setAttribute('data-chart-url', chartUrl);
        // sandbox 收紧图表 HTML 权限：仅放行脚本（Plotly 渲染所需），不带 allow-same-origin，
        // 使图表运行于 null origin，无法读取父页面 cookie / localStorage。
        iframe.setAttribute('sandbox', 'allow-scripts allow-popups');
        iframe.style.cssText = 'width:100%;height:400px;border:none;border-radius:8px;margin:8px 0;';
        const wrapper = document.createElement('div');
        wrapper.appendChild(iframe);
        chartsDiv.appendChild(wrapper);
      }
      return false;
    }

    // 正常内容：流式追加 —— 只重写 stream-content,不动 step-progress / charts
    // (commit 1 bug 修复:原 L743 bubble.innerHTML=... 销毁步骤清单 + 重复渲染的图表)
    if (thinking) {
      // 首次收到实际内容：关闭思考状态和转圈
      thinking = false;
      if (statusEl) statusEl.style.display = 'none';
    }
    if (fullText.length > 0) fullText += '\n';
    fullText += data;
    // Bug 2 修复:剥离 charts_dir PNG image 引用,避免 [CHART:url] iframe + <img> 双渲染
    // LLM 报告 markdown 里常含 ![对比图](/reports/charts/xxx.png) 引用 PNG,
    // 但 chat_stream.py:181 同时已发 [CHART:url] 帧——同一图渲染两次
    var contentDiv = streamContent();
    var rendered = _stripAlreadyRenderedCharts(renderMarkdown(fullText));
    contentDiv.innerHTML = rendered;
    _syncRaw(contentDiv, fullText);
    scrollToBottom();
    return false;
  }

  // ── 跨 chunk 行缓冲：一条 SSE 事件可能被网络切成多个 chunk， ──
  // ── 末尾不完整行留到 buffer，等下个 chunk 拼接后再处理     ──
  let buffer = '';
  let aborted = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        if (buffer) processLine(buffer);
        break;
      }
      resetIdle(); // 收到数据，重置空闲超时
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n');
      buffer = parts.pop(); // 末尾不完整行留待下次
      for (const line of parts) {
        if (processLine(line)) { aborted = true; break; } // [ERROR] 终止流
      }
      if (aborted) break;
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      if (statusEl) statusEl.style.display = 'none';
      // 用户主动停止：保留已生成内容，不打"超时"红字
      if (userStopped) {
        if (!fullText.trim()) bubble.innerHTML = '<span style="color:var(--color-ink-3)">已停止生成。</span>';
        return;
      }
      // 真·超时：无内容才提示
      if (!fullText.trim()) bubble.innerHTML = '<span style="color:var(--color-error)">请求超时，请重试。</span>';
      return;
    }
    throw err;
  } finally {
    clearTimeout(idleTimer);
  }

  // 处理完成后的状态
  if (statusEl) statusEl.style.display = 'none';

  if (!fullText.trim() && !errorRendered) {
    bubble.innerHTML = '收到空响应，请重试。';
    if (statusEl) statusEl.style.display = 'none';
  }
}

let _msgSeq = 0;   // 消息自增 id
function appendMessage(role, text) {
  var container = document.getElementById('chatContainer');
  var div = document.createElement('div');
  div.className = 'message ' + role;
  div.id = 'msg-' + (++_msgSeq);
  div.dataset.raw = text || '';
  var statusDiv = role === 'assistant'
    ? '<div class="chat-status" style="display:none"><span class="spinner"></span><span class="status-text"></span></div>'
    : '';
  var now = new Date();
  var ts = now.toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
  // 编辑按钮仅 user 消息显示
  var editBtn = role === 'user'
    ? '<button class="msg-action-btn" onclick="editMessage(this)" title="编辑并重新发送">✎ 编辑</button>'
    : '';
  var actions = '<div class="msg-actions">'
    + '<button class="msg-action-btn" onclick="copyMessage(this)" title="复制">⧉ 复制</button>'
    + editBtn
    + '</div>';
  var avatarHtml = role === 'user'
    ? (window.Icons ? window.Icons.user : '👤')
    : (window.Icons ? window.Icons.bot : '🤖');
  div.innerHTML = '<div class="avatar">' + avatarHtml + '</div>'
    + '<div class="bubble-wrap"><div class="bubble">' + escapeHtml(text) + '</div>'
    + actions
    + '<div class="msg-meta">' + ts + '</div></div>' + statusDiv;
  container.appendChild(div);
  return div;
}

// 同步 assistant 流式内容到 dataset.raw，供复制按钮取最新文本
function _syncRaw(bubble, text) {
  var m = bubble.closest('.message');
  if (m) m.dataset.raw = text;
}

// 复制消息原文
function copyMessage(btn) {
  var msg = btn.closest('.message');
  var raw = msg ? (msg.dataset.raw || '') : '';
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(raw).then(function() {
      showToast('已复制', 'success', 1500);
    }).catch(function() { _fallbackCopy(raw); });
  } else {
    _fallbackCopy(raw);
  }
}
function _fallbackCopy(text) {
  var ta = document.createElement('textarea');
  ta.value = text; document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); showToast('已复制', 'success', 1500); }
  catch(e) { showToast('复制失败', 'error', 2000); }
  document.body.removeChild(ta);
}

// 编辑用户消息并重新发送：删除该消息及之后所有消息（含未完成回复），用新文本重走一轮
function editMessage(btn) {
  var msg = btn.closest('.message');
  if (!msg) return;
  var bubble = msg.querySelector('.bubble');
  var raw = msg.dataset.raw || '';
  // 若正在生成，先停止（保留已生成内容，随后整段会被删除）
  if (isProcessing) stopGeneration();
  bubble.innerHTML = '<textarea class="edit-area"></textarea>'
    + '<div class="edit-actions"><button class="edit-send" onclick="submitEdit(this)">发送</button>'
    + '<button class="edit-cancel" onclick="cancelEdit(this)">取消</button></div>';
  var ta = bubble.querySelector('.edit-area');
  ta.value = raw;
  ta.focus();
  ta.setSelectionRange(raw.length, raw.length);
}
function submitEdit(btn) {
  var msg = btn.closest('.message');
  var ta = msg.querySelector('.edit-area');
  var newText = (ta.value || '').trim();
  if (!newText) { showToast('内容不能为空', 'error', 2000); return; }
  var container = document.getElementById('chatContainer');
  // 删除该消息及之后所有消息（含那条未完成/已停止的 assistant 回复）
  while (container.lastElementChild) {
    if (container.lastElementChild.id === msg.id) break;
    container.lastElementChild.remove();
  }
  if (msg) msg.remove();
  // 延迟重发，让上一次 streamChat 的 finally 先复位 isProcessing/按钮
  document.getElementById('userInput').value = newText;
  setTimeout(function() { sendMessage(); }, 120);
}
function cancelEdit(btn) {
  var msg = btn.closest('.message');
  var bubble = msg.querySelector('.bubble');
  // 还原原文（user 消息原文是纯文本，renderMarkdown 对纯文本安全）
  bubble.innerHTML = renderMarkdown(msg.dataset.raw || '');
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
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, function(_, lang, code) {
    return '<pre><button class="copy-btn" onclick="copyCode(this)">复制</button><code>' + code.trim() + '</code></pre>';
  });
  // 标题
  html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  // 粗体/斜体
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // 行内代码
  html = html.replace(/`(.+?)`/g, '<code>$1</code>');
  // 分隔线
  html = html.replace(/^---+$/gm, '<hr>');
  // 图片（协议白名单，非 http(s)/相对路径则丢弃 src）
  html = html.replace(/!\[(.*?)\]\((.*?)\)/g, function(_, alt, url) {
    var u = safeUrl(url); return u ? '<img src="' + u + '" alt="' + alt + '">' : alt;
  });
  // 链接（协议白名单）
  html = html.replace(/\[(.*?)\]\((.*?)\)/g, function(_, label, url) {
    var u = safeUrl(url);
    return u ? '<a href="' + u + '">' + label + '</a>' : label;
  });
  // 无序列表
  html = html.replace(/^- (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>[\s\S]*<\/li>)/, '<ul>$1</ul>');
  // 有序列表
  html = html.replace(/^\d+\. (.+)$/gm, '<li>$1</li>');
  // 引用
  html = html.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');
  // 段落
  html = html.replace(/\n\n/g, '</p><p>');
  html = '<p>' + html + '</p>';
  html = html.replace(/<p><\/p>/g, '');
  html = html.replace(/<p>(<[hHuol])/g, '$1');
  html = html.replace(/(<\/[hH]\d>|<\/[uo]l>)<\/p>/g, '$1');
  html = html.replace(/\n/g, '<br>');
  return html;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  // innerHTML 仅转义 & < >；补转义单/双引号以闭合 HTML 属性上下文（title="..." 等），
  // 防止用户可控内容（文件名等）突破属性边界注入。
  return div.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
    const r = await Auth.authedFetch('/api/knowledge/files', {headers: authHeaders()});
    if (!r.ok) { document.getElementById('kbFileList').innerHTML = '<div class="kb-file" style="color:var(--color-ink-3);justify-content:center;">加载失败</div>'; return; }
    const data = await r.json();
    const files = data.files || [];
    const list = document.getElementById('kbFileList');
    if (files.length === 0) {
      list.innerHTML = '<div class="kb-file" style="color:var(--color-ink-3);justify-content:center;">暂无知识库文件</div>';
    } else {
      list.innerHTML = files.map(f => {
        const badge = f.ingested
          ? '<span class="kb-badge in">已入库</span>'
          : '<span class="kb-badge out">待入库</span>';
        return `<div class="kb-file" title="${escapeHtml(f.filename)}">
          <span class="kb-name">${escapeHtml(f.filename)}</span>
          ${badge}
          <button class="kb-del" data-kb-file="${escapeHtml(f.filename)}" title="删除">✕</button>
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
    const r = await Auth.authedFetch('/api/knowledge/stats', {headers: authHeaders()});
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
    const r = await Auth.authedFetch('/api/knowledge/upload', {
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
    const r = await Auth.authedFetch('/api/knowledge/files/' + encodeURIComponent(filename), {
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
    const r = await Auth.authedFetch('/api/knowledge/reindex', {
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
    const r = await Auth.authedFetch('/api/datasets', {headers: authHeaders()});
    if (!r.ok) { document.getElementById('dsList').innerHTML = '<div class="ds-item" style="color:var(--color-ink-3);justify-content:center;">加载失败</div>'; return; }
    const data = await r.json();
    const datasets = data.datasets || [];
    document.getElementById('dsCount').textContent = datasets.length + ' 个';
    const list = document.getElementById('dsList');
    if (datasets.length === 0) {
      list.innerHTML = '<div class="ds-item" style="color:var(--color-ink-3);justify-content:center;">暂无数据集</div>';
    } else {
      list.innerHTML = datasets.map(d => {
        const rows = d.row_count > 0 ? d.row_count.toLocaleString() + '行' : '';
        const safeId = String(d.name).replace(/[^A-Za-z0-9_]/g,'_');
        // 侧边栏优先显示用户能认得的原始文件名（display_name，含中文），
        // 安全化表名（ds_202507242126）只作 hover title 兜底；二者都无则用 name。
        const showName = d.display_name || d.name;
        const titleTip = (d.display_name && d.display_name !== d.name)
          ? `${d.display_name}（表 ${d.name}）` : d.name;
        return `<div class="ds-item" data-ds-toggle="${escapeHtml(safeId)}">
          <span class="ds-icon">${dsIcon(d.source_type)}</span>
          <span class="ds-name" title="${escapeHtml(titleTip)}">${escapeHtml(showName)}</span>
          <span class="ds-rows">${rows}</span>
          <button class="ds-del" data-ds-del="${escapeHtml(d.name)}" title="删除">✕</button>
        </div>
        <div class="ds-detail" id="ds-detail-${escapeHtml(safeId)}">加载中...</div>`;
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
    const r = await Auth.authedFetch('/api/datasets/' + encodeURIComponent(name) + '/schema', {headers: authHeaders()});
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
    const r = await Auth.authedFetch('/api/datasets/upload', {
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
    loadAllFiles();          // CSV 入 DuckDB，文件管理列表也需同步
    _expandSection('Ds');    // 上传后自动展开数据集区块，避免折叠看不到
  } catch(err) { showToast('上传失败: ' + err.message, 'error', 4000); }
  e.target.value = '';
});

async function deleteDs(name) {
  if (!(await showConfirm('确认删除数据集「' + name + '」？\n将同时删除 DuckDB 表和本地文件。'))) return;
  try {
    const r = await Auth.authedFetch('/api/datasets/' + encodeURIComponent(name), {
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

// ── 账号设置（需求①） ──
var _setKeyEditing = false;
var _setThinkingInitial = false;   // 思考开关初始值：未改动则保存时不上传，保留 env/已存值
async function loadSettingsStatus() {
  // 登录后查是否已配置；未配置则弹提示横幅 + 侧边栏红点
  try {
    var r = await Auth.authedFetch('/api/settings/status', {headers: authHeaders()});
    var d = await r.json();
    var banner = document.getElementById('settingsBanner');
    var dot = document.getElementById('settingsDot');
    if (d.authed && !d.configured) {
      if (banner) banner.style.display = 'flex';
      if (dot) dot.classList.remove('ok');
    } else {
      if (banner) banner.style.display = 'none';
      if (dot) dot.classList.add('ok');
    }
  } catch(e) { console.log('加载配置状态失败:', e); }
}
async function loadSettings() {
  // 拉取掩码配置填表
  try {
    var r = await Auth.authedFetch('/api/settings', {headers: authHeaders()});
    if (r.status === 401) return;
    var d = await r.json();
    if (!d.configured) return;
    var s = d.settings || {};
    document.getElementById('setApiKey').value = s.llm_api_key || '';
    document.getElementById('setApiKey').type = 'password';
    document.getElementById('setChatModel').value = s.llm_model_name || '';
    document.getElementById('setEnableThinking').checked = !!s.llm_enable_thinking;
    _setThinkingInitial = !!s.llm_enable_thinking;   // null/undefined 视为关（未设置）
    document.getElementById('setBaseUrl').value = s.llm_base_url || '';
    document.getElementById('setEmbedModel').value = s.embedding_model_name || '';
    document.getElementById('setVdbHost').value = s.vector_db_host || '';
    document.getElementById('setVdbPort').value = s.vector_db_port || '';
    document.getElementById('setVdbCollection').value = s.vector_db_collection || '';
    document.getElementById('setLocalDb').value = s.local_db_conn || '';
    _setKeyEditing = false;
    document.getElementById('setKeyToggle').textContent = '编辑';
  } catch(e) { console.log('加载配置失败:', e); }
}
function toggleKeyEdit() {
  var inp = document.getElementById('setApiKey');
  var btn = document.getElementById('setKeyToggle');
  _setKeyEditing = !_setKeyEditing;
  if (_setKeyEditing) {
    // 进入编辑：清空掩码值，明文输入
    inp.value = '';
    inp.type = 'text';
    inp.placeholder = '输入新的 API Key（明文）';
    btn.textContent = '取消';
  } else {
    // 取消编辑：重新拉取掩码值
    loadSettings();
  }
}
function openSettingsPanel() {
  var body = document.getElementById('sectionBodySet');
  var chevron = document.getElementById('chevronSet');
  if (body) body.classList.remove('collapsed');
  if (chevron) chevron.classList.remove('collapsed');
  // 与 toggleSection('set') 展开行为一致：加载掩码配置，避免面板空白
  loadSettings();
}
async function saveSettings() {
  var payload = {
    llm_api_key: document.getElementById('setApiKey').value,
    llm_model_name: document.getElementById('setChatModel').value,
    llm_base_url: document.getElementById('setBaseUrl').value,
    embedding_model_name: document.getElementById('setEmbedModel').value,
    vector_db_host: document.getElementById('setVdbHost').value,
    vector_db_port: document.getElementById('setVdbPort').value,
    vector_db_collection: document.getElementById('setVdbCollection').value,
    local_db_conn: document.getElementById('setLocalDb').value
  };
  // 思考开关未改动则不上传：后端 COALESCE 保住已存值，不覆盖 .env/设置页默认
  var thinkNow = document.getElementById('setEnableThinking').checked;
  if (thinkNow !== _setThinkingInitial) payload.llm_enable_thinking = thinkNow;
  // 若未进入编辑模式且 key 含掩码标记，则不发送明文 key 字段（后端会保留旧值）
  try {
    var r = await Auth.authedFetch('/api/settings', {
      method: 'POST',
      headers: Object.assign({'Content-Type':'application/json'}, authHeaders()),
      body: JSON.stringify(payload)
    });
    var d = await r.json();
    if (d.ok) {
      showToast('配置已生效（无需重启）', 'success');
      await loadSettings();        // 刷新掩码展示
      await loadSettingsStatus();  // 刷新红点/横幅
    } else {
      showToast('保存失败：' + (d.error || '未知错误'), 'error');
    }
  } catch(e) {
    showToast('保存失败：' + e, 'error');
  }
}

// ── 文件管理（需求②：统一文本/表格 + 进度 + 状态轮询） ──
function toggleFilesSection() {
  var body = document.getElementById('sectionBodyFiles');
  var chevron = document.getElementById('chevronFiles');
  if (!body || !chevron) return;
  body.classList.toggle('collapsed');
  chevron.classList.toggle('collapsed');
  if (!body.classList.contains('collapsed')) loadAllFiles();
}
async function loadAllFiles() {
  try {
    var r = await Auth.authedFetch('/api/files', {headers: authHeaders()});
    if (!r.ok) {
      document.getElementById('allFileList').innerHTML =
        '<div class="kb-file" style="color:var(--color-ink-3);justify-content:center;">加载失败</div>';
      return;
    }
    var data = await r.json();
    var files = data.files || [];
    document.getElementById('filesCount').textContent = files.length;
    var list = document.getElementById('allFileList');
    if (files.length === 0) {
      list.innerHTML = '<div class="kb-file" style="color:var(--color-ink-3);justify-content:center;">暂无文件</div>';
      return;
    }
    list.innerHTML = files.map(function(f) {
      var icon = f.type === 'table' ? '📊' : '📄';
      var badge = f.status === '已完成'
        ? '<span class="kb-badge in">完成</span>'
        : (f.status === '失败'
            ? '<span class="kb-badge" style="background:var(--color-error-surface);color:var(--color-paper);">失败</span>'
            : '<span class="kb-badge out">处理中</span>');
      var sizeStr = f.size ? fmtSize(f.size) : '';
      var delType = f.type === 'table' ? 'table' : 'text';
      var tail = f.type === 'table'
        ? (f.table_name ? '<span style="font-size:10px;color:var(--color-ink-3);">表:' + escapeHtml(f.table_name) + '</span>' : '')
        : (sizeStr ? '<span style="font-size:10px;color:var(--color-ink-3);">' + sizeStr + '</span>' : '');
      return '<div class="kb-file" title="' + escapeHtml(f.name) + '">' +
        '<span style="flex-shrink:0;">' + icon + '</span>' +
        '<span class="kb-name">' + escapeHtml(f.name) + '</span>' +
        tail + badge +
        '<button class="kb-del" data-file-name="' + escapeHtml(f.name) + '" data-file-type="' + delType + '" title="删除">✕</button>' +
        '</div>';
    }).join('');
  } catch(e) {
    console.log('加载文件列表失败:', e);
  }
}
function _routeFileByExt(fname) {
  var ext = (fname.split('.').pop() || '').toLowerCase();
  if (['csv','xlsx','xls'].indexOf(ext) >= 0) return {kind:'table', endpoint:'/api/datasets/upload', field:'file'};
  return {kind:'text', endpoint:'/api/knowledge/upload', field:'files'};
}
function _uploadOneWithProgress(file, progEl) {
  return new Promise(function(resolve) {
    var route = _routeFileByExt(file.name);
    // 大文件阈值：PDF/Excel > 50MB 给提示
    var bigExts = ['pdf','xlsx','xls'];
    var ext = (file.name.split('.').pop() || '').toLowerCase();
    if (bigExts.indexOf(ext) >= 0 && file.size > 50 * 1024 * 1024) {
      var mb = Math.round(file.size / 1024 / 1024);
      progEl.textContent = '⚠️ ' + file.name + '（' + mb + 'MB）较大，预计解析较慢，正在上传...';
    }
    var xhr = new XMLHttpRequest();
    var fd = new FormData();
    fd.append(route.field, file);
    xhr.open('POST', route.endpoint);
    xhr.setRequestHeader('Authorization', 'Bearer ' + authToken);
    xhr.upload.onprogress = function(e) {
      if (e.lengthComputable) {
        var pct = Math.round(e.loaded * 100 / e.total);
        progEl.textContent = '上传中 ' + file.name + '：' + pct + '%';
      }
    };
    xhr.onload = function() {
      try {
        var data = JSON.parse(xhr.responseText);
        progEl.textContent = '';
        // 文本类上传结果在 data.results[]，提取 advisory
        if (data && data.results) {
          data.results.forEach(function(r) {
            if (r.ok !== false && r.success !== false && r.advisory) {
              showToast(r.advisory, 'info', 5000);
            }
          });
        }
        resolve({ok: xhr.status >= 200 && xhr.status < 300, data: data, file: file.name, kind: route.kind});
      } catch(e) {
        progEl.textContent = '';
        resolve({ok: false, error: '响应解析失败', file: file.name});
      }
    };
    xhr.onerror = function() {
      progEl.textContent = '';
      resolve({ok: false, error: '网络错误', file: file.name});
    };
    xhr.send(fd);
  });
}
document.getElementById('allFileInput').addEventListener('change', async function(e) {
  var files = Array.from(e.target.files || []);
  if (!files.length) return;
  var prog = document.getElementById('uploadProgress');
  var results = [];
  for (var i = 0; i < files.length; i++) {
    var r = await _uploadOneWithProgress(files[i], prog);
    results.push(r);
  }
  var ok = results.filter(function(x){return x.ok;}).length;
  var fails = results.filter(function(x){return !x.ok;});
  if (ok) showToast('上传完成：成功 ' + ok + ' / ' + files.length + ' 个', 'success');
  fails.forEach(function(f) {
    var msg = (f.data && f.data.error) || f.error || '失败';
    showToast(f.file + ' 上传失败：' + msg, 'error', 5000);
  });
  // 批量上传对应关系：逐个展示「原始文件名 → 数据集显示名/表名」，让用户能对上号
  results.filter(function(x){return x.ok && x.kind === 'table' && x.data && x.data.name;}).forEach(function(r) {
    var disp = r.data.display_name || r.data.name;
    var arrow = (disp !== r.file) ? (' → 数据集「' + disp + '」') : (' → 数据集「' + disp + '」');
    showToast('✓ ' + r.file + arrow, 'info', 6000);
  });
  loadAllFiles();
  loadDatasets();           // CSV 类经路由入 DuckDB，数据集列表也需同步
  _expandSection('Files');  // 上传后自动展开文件管理区块
  // 若有处理中项，轮询直到全部完成或超时
  _pollFilesStatus(60000);
  e.target.value = '';
});
function _pollFilesStatus(timeoutMs) {
  var start = Date.now();
  function tick() {
    if (Date.now() - start > timeoutMs) return;
    Auth.authedFetch('/api/files', {headers: authHeaders()}).then(function(r){return r.json();}).then(function(data) {
      var pending = (data.files || []).some(function(f){return f.status === '处理中';});
      loadAllFiles();
      loadDatasets();       // 轮询期间数据集可能就绪，同步刷新
      if (pending) setTimeout(tick, 2000);
    }).catch(function(){ /* ignore */ });
  }
  setTimeout(tick, 1500);
}
async function deleteFile(name, type) {
  if (!(await showConfirm('确认删除「' + name + '」？\n' + (type === 'table' ? '将同时删除 DuckDB 表和本地文件' : '将从向量库移除对应分片')))) return;
  var url = type === 'table'
    ? '/api/datasets/' + encodeURIComponent(name)
    : '/api/knowledge/files/' + encodeURIComponent(name);
  try {
    var r = await Auth.authedFetch(url, {method: 'DELETE', headers: authHeaders()});
    var data = await r.json();
    if (data.success !== undefined && !data.success) {
      showToast(data.error || '删除失败', 'error', 4000); return;
    }
    showToast('已删除「' + name + '」', 'success');
    loadAllFiles();
  } catch(e) { showToast('删除失败：' + e.message, 'error', 4000); }
}

// ── 初始化 ──
// 事件委托：文件名等用户可控内容不再拼进 onclick 内联 JS（属性上下文下 HTML 实体会被解码，
// 仅转义不足以防 XSS），改为 data-* 属性 + 委托点击，从根上杜绝存储型 XSS。
(function initDelegatedHandlers() {
  var kbList = document.getElementById('kbFileList');
  if (kbList) kbList.addEventListener('click', function(e) {
    var btn = e.target.closest('.kb-del');
    if (btn && btn.dataset.kbFile) deleteKbFile(btn.dataset.kbFile);
  });
  var allList = document.getElementById('allFileList');
  if (allList) allList.addEventListener('click', function(e) {
    var btn = e.target.closest('.kb-del');
    if (btn && btn.dataset.fileName) deleteFile(btn.dataset.fileName, btn.dataset.fileType);
  });
  var dsListEl = document.getElementById('dsList');
  if (dsListEl) dsListEl.addEventListener('click', function(e) {
    var delBtn = e.target.closest('.ds-del');
    if (delBtn) { e.stopPropagation(); deleteDs(delBtn.dataset.dsDel); return; }
    var item = e.target.closest('.ds-item');
    if (item && item.dataset.dsToggle) toggleDsDetail(item.dataset.dsToggle);
  });
})();

// ── 图表标记解析：从存储文本中提取 [CHART:url] 并渲染为 iframe ──
// 返回 { text: 剥离标记后的纯文本, chartUrls: 图表 URL 数组 }
function _extractCharts(text) {
  var chartTok = SSE_PROTOCOL.CHART.slice(1, -1);   // 'CHART'（词表取词，禁散写字面量）
  var chartUrls = [];
  var re = new RegExp('\\[' + chartTok + ':([^\\]]+)\\]', 'g');
  var m;
  while ((m = re.exec(text)) !== null) {
    var url = m[1].trim();
    if (url && url.charAt(0) === '/' && !url.startsWith('//')) {
      chartUrls.push(url);
    }
  }
  var cleaned = text.replace(new RegExp('\\[' + chartTok + ':[^\\]]+\\]\\n*', 'g'), '').trim();
  return { text: cleaned, chartUrls: chartUrls };
}

// 在 bubble 中渲染图表 iframe（chartUrls 为 URL 数组）
function _renderChartIframes(bubble, chartUrls) {
  chartUrls.forEach(function(url) {
    var iframe = document.createElement('iframe');
    iframe.src = url;
    iframe.setAttribute('sandbox', 'allow-scripts allow-popups');
    iframe.style.cssText = 'width:100%;height:400px;border:none;border-radius:8px;margin:8px 0;';
    bubble.appendChild(iframe);
  });
}

// _stripAlreadyRenderedCharts / cssEscape 已抽至 stream_dedup.js（P1-1，node 可单测），
// 由 app.html 在本文件之前引入；浏览器全局函数签名与调用点保持不变。


// ── 报告导出：在报告 bubble 末尾追加导出按钮栏 ──
function appendExportBar(bubble, markdown) {
  // 标题从 markdown 首行 # 解析，回退到默认
  var title = '数据分析报告';
  var m = markdown.match(/^#\s+(.+)$/m);
  if (m) title = m[1].trim();
  var bar = document.createElement('div');
  bar.className = 'export-bar';
  [
    { fmt: 'md',   label: 'Markdown' },
    { fmt: 'docx', label: 'Word' },
    { fmt: 'pdf',  label: 'PDF' },
    { fmt: 'html', label: 'HTML' },
  ].forEach(function (item) {
    var btn = document.createElement('button');
    btn.className = 'export-btn';
    btn.textContent = '导出 ' + item.label;
    btn.onclick = function () { doExport(btn, markdown, title, item.fmt); };
    bar.appendChild(btn);
  });
  bubble.appendChild(bar);
  scrollToBottom();
}

function doExport(btn, markdown, title, fmt) {
  var tip = document.createElement('span');
  tip.className = 'export-tip';
  btn.disabled = true;
  var oldText = btn.textContent;
  btn.textContent = '生成中...';
  fetch('/api/report/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ markdown: markdown, title: title, format: fmt }),
  }).then(function (resp) {
    if (!resp.ok) return resp.text().then(function (t) { throw new Error(t || ('HTTP ' + resp.status)); });
    return resp.blob();
  }).then(function (blob) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = (title || 'report') + '.' + (fmt === 'docx' ? 'docx' : fmt);
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }).catch(function (err) {
    tip.textContent = '导出失败: ' + err.message;
    btn.parentElement.appendChild(tip);
    setTimeout(function () { tip.remove(); }, 4000);
  }).finally(function () {
    btn.disabled = false;
    btn.textContent = oldText;
  });
}

loadSessions();
loadKbFiles();
loadSettingsStatus();
loadAllFiles();
document.getElementById('userInput').focus();
