// ── Bug 2 修复辅助：剥离已被 [CHART:url] 帧渲染过的 PNG image 引用 ──
// LLM 报告 markdown 经常含 ![xxx](/reports/charts/xxx.png) 引用 PNG,
// 但 chat_stream.py:181 同时已发 [CHART:url] 帧(stream 路径)→ 同图渲染两次。
// 此函数:扫描 stream-charts div 已渲染 iframe 的 url,在 markdown HTML 中
// 剥离对应 <img src=...> 元素(同源 .png/.html basename 共享)。
//
// P1-1:从 app.js 抽出为独立文件(UMD,浏览器挂全局 / node 可 require 单测),
// 函数名与调用签名保持兼容。node 环境下无 document 时退化为原样返回(安全)。
(function (root) {
  'use strict';

  function _stripAlreadyRenderedCharts(html, doc) {
    try {
      var renderedUrls = {};
      var container = doc || (typeof document !== 'undefined' ? document : null);
      if (!container) return html;
      var iframes = container.querySelectorAll('.stream-charts iframe[data-chart-url]');
      for (var i = 0; i < iframes.length; i++) {
        renderedUrls[iframes[i].getAttribute('data-chart-url')] = 1;
      }
      if (Object.keys(renderedUrls).length === 0) return html;
      var keys = Object.keys(renderedUrls);
      for (var j = 0; j < keys.length; j++) {
        var url = keys[j];
        // url 形如 /reports/charts/bar_xxx.html;PNG 在同名 .png
        var pngUrl = url.replace(/\.html?$/, '.png');
        var escaped = pngUrl.replace(/[\/.]/g, '\$&');
        var re = new RegExp('<img[^>]*src=["\']' + escaped + '["\'][^>]*>', 'g');
        html = html.replace(re, '');
      }
      return html;
    } catch (e) { return html; }
  }

  // CSS.escape polyfill(部分老浏览器没原生)
  function cssEscape(s) {
    var w = typeof window !== 'undefined' ? window : null;
    if (w && w.CSS && w.CSS.escape) return w.CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, function (c) { return '\\' + c.charCodeAt(0).toString(16) + ' '; });
  }

  var api = {
    _stripAlreadyRenderedCharts: _stripAlreadyRenderedCharts,
    cssEscape: cssEscape,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;                 // node:test 单测
  } else {
    root._stripAlreadyRenderedCharts = _stripAlreadyRenderedCharts;  // 浏览器全局(app.js 调用点不变)
    root.cssEscape = cssEscape;
  }
})(typeof window !== 'undefined' ? window : this);
