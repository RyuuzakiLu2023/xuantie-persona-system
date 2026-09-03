/* ================================================================
   照渊台 · 挂载入口（总导游图）

  ▍这是什么
  ① 干什么：把 App 挂到 index.html 的 #root 上；样式的加载顺序在这里定死
  ② 给谁看：改样式加载顺序、加全局 provider 的人
  ③ 依赖：styles/tokens.css → base.css → app.css（顺序不能换，见下）

  ▍★ 样式顺序不能换 ★
  tokens 先（定义变量）→ base 后（用变量做重置与零件）→ app 最后（布局与视图）。
  换了顺序就会出现"变量还没定义就被用"，浏览器静默丢弃 → 界面看起来没坏但缺了定义
  （血泪同源：见 错题本.md #3，CSS 拼错不报错只消失）。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T1–T4）*
================================================================ */

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles/tokens.css';
import './styles/base.css';
import './styles/app.css';
import { App } from './App';

const host = document.getElementById('root');
if (!host) throw new Error('#root 不在——index.html 被改坏了');

/* StrictMode 会在开发下双跑一次 effect：这是故意留着的体检。
   装置的 effect 必须能被 dispose 干净地拆一次再装一次，装不干净就是漏。 */
createRoot(host).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
