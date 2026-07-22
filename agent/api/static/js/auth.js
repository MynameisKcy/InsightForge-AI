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
