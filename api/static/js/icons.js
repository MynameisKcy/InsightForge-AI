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
      + '</svg>',
    // GitHub：官方 Octocat 标志（实心剪影，fill=currentColor；与线性图标同色系，由父级 CSS 控色）
    github: '<svg class="ic ic-github" width="24" height="24" viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true">'
      + '<path d="M12 .5C5.73.5.5 5.73.5 12c0 5.08 3.29 9.39 7.86 10.91.58.11.79-.25.79-.56 0-.28-.01-1.02-.02-2-3.2.7-3.88-1.54-3.88-1.54-.53-1.34-1.3-1.7-1.3-1.7-1.06-.72.08-.71.08-.71 1.17.08 1.79 1.2 1.79 1.2 1.04 1.79 2.73 1.27 3.4.97.11-.76.41-1.27.74-1.56-2.55-.29-5.23-1.28-5.23-5.7 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11 11 0 0 1 5.8 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.84 1.19 3.1 0 4.43-2.69 5.41-5.25 5.69.42.36.79 1.08.79 2.18 0 1.58-.01 2.85-.01 3.24 0 .31.21.68.8.56A11.51 11.51 0 0 0 23.5 12C23.5 5.73 18.27.5 12 .5z"/>'
      + '</svg>',
    // 太阳：圆心+八方射线（日间模式标识）
    sun: '<svg class="ic ic-sun" width="24" height="24" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<circle cx="14" cy="14" r="5"/>'
      + '<path d="M14 3v3M14 22v3M3 14h3M22 14h3M5.6 5.6l2.1 2.1M20.3 20.3l2.1 2.1M22.4 5.6l-2.1 2.1M7.7 20.3l-2.1 2.1"/>'
      + '</svg>',
    // 月亮：弦月（暗夜模式标识）
    moon: '<svg class="ic ic-moon" width="24" height="24" viewBox="0 0 28 28" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      + '<path d="M22.5 16.5A9.5 9.5 0 0 1 11.5 5.5a9.5 9.5 0 1 0 11 11z"/>'
      + '</svg>'
  };
})();
