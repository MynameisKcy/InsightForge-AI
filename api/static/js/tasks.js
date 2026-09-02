/* Task System（#1）前端最小入口：聊天页顶部「继续上次分析」。
 *
 * 刻意独立成文件（不并入 app.js）：app.js 存在未提交 WIP（流式分区重构），
 * 本脚本零侵入——只读 app.js 的全局函数 appendMessage/renderMarkdown/showToast/authHeaders。
 * 行为：进入页面拉取最新任务，若存在未完成（running/failed）任务则显示一条
 * 续跑条；点击 → POST /api/tasks/{id}/resume → 渲染报告 markdown。
 */
(function () {
  function el(id) { return document.getElementById(id); }

  function findResumable(tasks) {
    for (var i = 0; i < tasks.length; i++) {
      if (tasks[i].status === 'running' || tasks[i].status === 'failed') return tasks[i];
    }
    return null;
  }

  function authHeaders() {
    if (window.authHeaders) return window.authHeaders();
    var t = localStorage.getItem('token');
    return t ? { 'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json' } : { 'Content-Type': 'application/json' };
  }

  function renderReportResult(result) {
    var report = (result && result.report) || {};
    var markdown = report.markdown || '';
    if (markdown) {
      var msg = appendMessage('assistant', '');
      var bubble = msg.querySelector('.bubble');
      bubble.innerHTML = renderMarkdown(markdown);
      scrollToBottom();
    } else if (result && !result.success) {
      showToast(result.error || '续跑失败', 'error', 3000);
    }
  }

  function attachResumeBar(task) {
    var inputArea = document.querySelector('.input-area');
    if (!inputArea || el('taskResumeBar')) return;
    var bar = document.createElement('div');
    bar.id = 'taskResumeBar';
    bar.style.cssText = 'display:flex;align-items:center;gap:10px;padding:8px 16px;' +
      'margin:0 auto 8px;max-width:720px;background:#E1F5EE;border:1px solid #9FE1CB;' +
      'border-radius:10px;font-size:13px;color:#085041;';
    var label = document.createElement('span');
    label.style.flex = '1';
    label.textContent = '上次分析未完成：「' + (task.title || '未命名任务') + '」（已完成 ' +
      task.completed + '/' + task.total + ' 步）';
    var btn = document.createElement('button');
    btn.textContent = '继续分析';
    btn.style.cssText = 'padding:4px 14px;border:none;border-radius:6px;' +
      'background:#0F6E56;color:#fff;cursor:pointer;font-size:13px;';
    btn.onclick = function () {
      btn.disabled = true; btn.textContent = '续跑中…';
      fetch('/api/tasks/' + encodeURIComponent(task.id) + '/resume', {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify({}),
      }).then(function (r) { return r.json(); }).then(function (data) {
        if (data && data.error && !data.report) { throw new Error(data.error); }
        renderReportResult(data);
        bar.style.display = 'none';
      }).catch(function (e) {
        btn.disabled = false; btn.textContent = '继续分析';
        showToast(e.message || '续跑失败，请重试', 'error', 3000);
      });
    };
    bar.appendChild(label);
    bar.appendChild(btn);
    inputArea.parentNode.insertBefore(bar, inputArea);
  }

  function init() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', init); return;
    }
    fetch('/api/tasks?limit=5', { headers: authHeaders() })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var task = findResumable((data && data.tasks) || []);
        if (task) attachResumeBar(task);
      })
      .catch(function () { /* 任务接口不可用时静默降级，不影响聊天主流程 */ });
  }

  init();
})();
