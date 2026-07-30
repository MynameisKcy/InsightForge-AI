// landing.js — 落地页登录/注册弹窗逻辑。
// 依赖 window.Auth（auth.js）提供 setToken/fetchMe。
(function () {
  'use strict';

  const modal = document.getElementById('auth-modal');
  const form = document.getElementById('auth-form');
  const modeInput = document.getElementById('auth-mode');
  const titleEl = document.getElementById('auth-title');
  const subEl = document.getElementById('auth-sub');
  const accountEl = document.getElementById('auth-account');
  const passwordEl = document.getElementById('auth-password');
  const password2El = document.getElementById('auth-password2');
  const password2Group = document.getElementById('auth-password2-group');
  const rememberEl = document.getElementById('auth-remember');
  const submitBtn = document.getElementById('auth-submit');
  const errorEl = document.getElementById('auth-error');
  const toggleEl = document.getElementById('auth-toggle');
  const btnLogin = document.getElementById('btn-login');
  const btnStart = document.getElementById('btn-start');
  const btnWorkbench = document.getElementById('btn-workbench');

  const LOGIN_TEXT = { title: '登录', sub: '登录以使用数据分析服务',
                       submit: '登 录', toggle: '没有账号？点击注册' };
  const REGISTER_TEXT = { title: '注册', sub: '创建账号以使用数据分析服务',
                          submit: '注 册', toggle: '已有账号？点击登录' };

  function setMode(mode) {
    modeInput.dataset.mode = mode;
    const isLogin = mode === 'login';
    password2Group.style.display = isLogin ? 'none' : 'block';
    password2El.required = !isLogin;
    const t = isLogin ? LOGIN_TEXT : REGISTER_TEXT;
    titleEl.innerHTML = (window.Icons ? window.Icons.bot : '🤖') + '<span>' + t.title + '</span>';
    subEl.textContent = t.sub;
    submitBtn.textContent = t.submit;
    toggleEl.textContent = t.toggle;
    hideError();
  }

  function openModal(mode) {
    setMode(mode);
    errorEl.textContent = '';
    errorEl.style.display = 'none';
    modal.classList.remove('hidden');
    accountEl.focus();
  }

  function closeModal() {
    modal.classList.add('hidden');
    hideError();
  }

  function hideError() {
    errorEl.textContent = '';
    errorEl.style.display = 'none';
  }

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.style.display = 'block';
  }

  // 点击遮罩空白处关闭弹窗
  modal.addEventListener('click', function (e) {
    if (e.target === modal) closeModal();
  });

  // Esc 关闭
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeModal();
  });

  if (btnLogin) btnLogin.addEventListener('click', function () { openModal('login'); });
  if (btnStart) btnStart.addEventListener('click', function () { openModal('register'); });
  // Hero 区「立即开始」按钮 — 同样打开注册弹窗
  const heroStart = document.getElementById('hero-start');
  if (heroStart) heroStart.addEventListener('click', function () { openModal('register'); });
  if (toggleEl) toggleEl.addEventListener('click', function (e) {
    e.preventDefault();
    const next = modeInput.dataset.mode === 'login' ? 'register' : 'login';
    setMode(next);
  });

  // 表单提交 — 公开端点，用原生 fetch（不带 Authorization 头）
  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    hideError();
    const mode = modeInput.dataset.mode;
    const account = accountEl.value.trim();
    const password = passwordEl.value;
    if (!account || !password) { showError('请输入账号和密码'); return; }
    if (mode === 'register') {
      const password2 = password2El.value;
      if (!password2) { showError('请再次输入密码'); return; }
      if (password !== password2) { showError('两次密码输入不一致'); return; }
    }

    const url = mode === 'login' ? '/api/login' : '/api/register';
    const body = { account: account, password: password };
    // 登录时把「记住我」传给后端，使会话 cookie 生命周期与前端存储一致：
    // 勾选 → 持久 cookie（24h）；不勾选 → 会话 cookie（关闭浏览器即失效）。
    if (mode === 'login') body.remember = rememberEl.checked;
    if (mode === 'register') body.password2 = password2El.value;

    submitBtn.disabled = true;
    const originalText = submitBtn.textContent;
    submitBtn.textContent = mode === 'login' ? '登录中...' : '注册中...';
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      let data = null;
      try { data = await res.json(); } catch (_) { /* ignore parse error */ }
      if (data && data.success && data.token) {
        // setToken(remember) — remember=true 写 localStorage，否则 sessionStorage
        if (window.Auth && typeof window.Auth.setToken === 'function') {
          window.Auth.setToken(data.token, rememberEl.checked);
        }
        window.location.href = '/app';
      } else if (data && data.success && !data.token) {
        // 注册成功但自动登录失败（无 token）— 提示手动登录
        showError(data.message || '操作成功，请手动登录');
        setMode('login');
      } else {
        showError((data && data.error) || '操作失败，请重试');
      }
    } catch (err) {
      showError('网络错误: ' + (err && err.message ? err.message : '未知错误'));
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = originalText;
    }
  });

  // 页面加载 — 已登录态切换导航按钮
  (async function initAuthState() {
    if (!window.Auth || typeof window.Auth.fetchMe !== 'function') return;
    const user = await window.Auth.fetchMe();
    if (user && user.account) {
      if (btnLogin) btnLogin.classList.add('hidden');
      if (btnStart) btnStart.classList.add('hidden');
      if (btnWorkbench) btnWorkbench.classList.remove('hidden');
    }
  })();
})();

// ── 主题切换（日间 / 暗夜）──────────────────────────────────────────
// 防 FOUC 的初值已由 index.html <head> 内联脚本在首绘前写好 <html data-theme>。
// 这里只负责：按钮交互、aria 同步、持久化，以及「未显式选择时跟随系统偏好」。
(function () {
  'use strict';
  var root = document.documentElement;
  var toggle = document.getElementById('theme-toggle');
  if (!toggle) return;

  function currentTheme() { return root.getAttribute('data-theme') === 'light' ? 'light' : 'dark'; }
  function syncAria() { toggle.setAttribute('aria-pressed', currentTheme() === 'light' ? 'true' : 'false'); }
  function apply(theme) { root.setAttribute('data-theme', theme); syncAria(); }

  // 初值同步按钮 aria-pressed（滑块位置由 CSS 据 [data-theme] 决定，首绘即正确）
  syncAria();

  toggle.addEventListener('click', function () {
    var next = currentTheme() === 'light' ? 'dark' : 'light';
    apply(next);
    try { localStorage.setItem('if-theme', next); } catch (e) { /* 配额禁用等，忽略 */ }
  });

  // 未显式选择时跟随系统主题变化；一旦用户手动切换（if-theme 已写入），停止跟随。
  try {
    var mq = window.matchMedia('(prefers-color-scheme: light)');
    var onSysChange = function (e) {
      if (localStorage.getItem('if-theme')) return;   // 已显式选择，不再跟随系统
      apply(e.matches ? 'light' : 'dark');
    };
    if (mq.addEventListener) mq.addEventListener('change', onSysChange);
    else if (mq.addListener) mq.addListener(onSysChange);   // 旧版 Safari
  } catch (e) { /* matchMedia 不可用则忽略 */ }
})();
