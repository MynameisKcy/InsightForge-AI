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

  // 鉴权失败只处理一次：多个并发请求同时 401 时，只触发一次跳转/清 token，
  // 避免主应用加载时 loadProfile+loadSettings 等并发请求各自通知/跳转。
  var handlingExpired = false;

  async function authedFetch(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({}, opts.headers || {}, {
      'Authorization': 'Bearer ' + getToken(),
    });
    const res = await fetch(url, opts);
    if (res.status === 401) {
      // 落地页（/）不需要鉴权 —— 401 在落地页上只清 token，不跳转、不循环。
      // 主应用（/app 等）鉴权失败 —— 只通知一次并跳回落地页。
      if (!handlingExpired) {
        handlingExpired = true;
        clearToken();
        if (window.location.pathname !== '/') {
          window.location.href = '/';
        } else {
          // 已在落地页：重置标志，允许后续（如手动登录）正常工作
          handlingExpired = false;
        }
      }
      return res;
    }
    return res;
  }

  async function fetchMe() {
    // 落地页加载时调用。没有 token 就不发请求 —— 落地页本就公开，
    // 无谓的 /api/me 401 请求会导致页面反复重载（死循环）。
    if (!getToken()) return null;
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
