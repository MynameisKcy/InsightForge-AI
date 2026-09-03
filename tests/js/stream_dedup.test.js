// P1-1:前端图表去重函数单测(node:test,零第三方依赖)。
// 被测对象为独立 UMD 文件 stream_dedup.js(浏览器全局 + node require)。
// 运行:node --test tests/js/
'use strict';

const { test } = require('node:test');
const assert = require('node:assert');

const dedup = require('../../api/static/js/stream_dedup.js');

// ── cssEscape(无 window 环境走 polyfill 路径) ──

test('cssEscape keeps identifier-safe characters untouched', () => {
  assert.strictEqual(dedup.cssEscape('abc_123-'), 'abc_123-');
  assert.strictEqual(dedup.cssEscape(''), '');
});

test('cssEscape escapes dots and slashes (hex escape) in node (no window.CSS)', () => {
  const out = dedup.cssEscape('/reports/charts/bar_1.html');
  assert.ok(!out.includes('/'), 'slash must be escaped');
  assert.ok(!out.includes('.'), 'dot must be escaped');
  assert.ok(out.includes('2f'), 'slash hex 2f present');
  assert.ok(out.includes('2e'), 'dot hex 2e present');
});

// ── _stripAlreadyRenderedCharts ──

function iframeDoc(urls) {
  // 最小 document stub:querySelectorAll 返回伪 NodeList(有 length + 索引访问)
  return {
    querySelectorAll() {
      return urls.map((u) => ({ getAttribute: () => u }));
    },
  };
}

test('returns html unchanged when no doc (node degrade path)', () => {
  const html = '<p>![x](/reports/charts/bar_1.png)</p>';
  assert.strictEqual(dedup._stripAlreadyRenderedCharts(html), html);
});

test('returns html unchanged when no iframe already rendered', () => {
  const html = '<p>![x](/reports/charts/bar_1.png)</p>';
  assert.strictEqual(dedup._stripAlreadyRenderedCharts(html, iframeDoc([])), html);
});

test('strips img whose .png has a rendered .html counterpart', () => {
  const html =
    '<p>text</p><img src="/reports/charts/bar_1.png" alt="对比图">' +
    '<img src="/reports/charts/bar_2.png" alt="保留">';
  const out = dedup._stripAlreadyRenderedCharts(html, iframeDoc(['/reports/charts/bar_1.html']));
  assert.ok(!out.includes('bar_1.png'), 'rendered chart png must be stripped');
  assert.ok(out.includes('bar_2.png'), 'unrendered chart png must be kept');
  assert.ok(out.includes('text'), 'surrounding content preserved');
});

test('strips multiple rendered charts and keeps non-chart content', () => {
  const html =
    '<img src="/reports/charts/a.png"><img src="/reports/charts/b.png">' +
    '<p>结论</p>';
  const out = dedup._stripAlreadyRenderedCharts(
    html, iframeDoc(['/reports/charts/a.html', '/reports/charts/b.html']));
  assert.ok(!out.includes('a.png') && !out.includes('b.png'));
  assert.ok(out.includes('结论'));
});

test('handles malformed html without throwing', () => {
  const html = null;
  // catch 分支:非字符串进 try,doc 无时原样;给 doc 且 html null → regex 报错被 catch 吞
  const out = dedup._stripAlreadyRenderedCharts(html, iframeDoc(['/reports/charts/a.html']));
  assert.strictEqual(out, null);
});
