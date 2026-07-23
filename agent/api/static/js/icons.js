// icons.js - 共享内联 SVG 图标（线性科技风，与落地页 feature-card 同语言）。
// 暴露 window.Icons.bot / window.Icons.user，供 app.js（聊天气泡+侧边栏）与
// landing.js（登录/注册标题）复用，避免重复定义。SVG 用 stroke=currentColor，
// 颜色由父级 .avatar / 标题 CSS 决定。
(function () {
  'use strict';
  window.Icons = {
    // 机器人：天线+圆点头+双眼+口线+双侧节点
    // width/height 属性是兜底默认尺寸（防 inline SVG 无 CSS 约束时撑满容器）；
    // 各处 CSS（.user-info svg / .avatar svg / h2 svg）优先级更高，仍可精确覆盖。
    bot: '<svg class="ic ic-bot" width="24" height="24" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M14 4.5v3"/>'
      + '<circle cx="14" cy="3.5" r="1.1" fill="currentColor" stroke="none"/>'
      + '<rect x="6.5" y="7.5" width="15" height="13" rx="2.5"/>'
      + '<circle cx="11" cy="13.5" r="1.3" fill="currentColor" stroke="none"/>'
      + '<circle cx="17" cy="13.5" r="1.3" fill="currentColor" stroke="none"/>'
      + '<path d="M11 17.5h6" stroke-linecap="round"/>'
      + '<path d="M4 12.5v3M24 12.5v3" stroke-linecap="round"/>'
      + '</svg>',
    // 用户：圆头+肩部弧线
    user: '<svg class="ic ic-user" width="24" height="24" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<circle cx="14" cy="10" r="4"/>'
      + '<path d="M5.5 23c0-4.7 3.8-7.5 8.5-7.5s8.5 2.8 8.5 7.5"/>'
      + '</svg>'
  };
})();
