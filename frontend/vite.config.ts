/* ================================================================
   XTMEM 照渊台 · Vite 构建配置（总导游图）

  ▍这是什么（固定四问）
  ① 干什么：把 React 19 + TS 源码打成纯静态站（后端还不存在，前端契约先行）
  ② 技术栈：Vite 8 + @vitejs/plugin-react；零 UI 库、零动画库、零图表库
  ③ 依赖：只有 react / react-dom。核心装置「渊」是自绘 Canvas 2D，纹样是内联 SVG
  ④ 给谁看：改构建配置、接真后端、加代理的人

  ▍文件从上到下的结构
  base          './' —— 产物可丢到任意子路径静态托管，不假设挂在域名根
  server.proxy  mock 开关关掉后，把 /api 转给真后端（默认 127.0.0.1:20000，即 api_server.py）
  build         target 定在 es2022，够用且不牵 polyfill

  ▍修改铁律（血泪原因写在后面）
  1. 【不】引入需要密钥的服务，也【不】引在线字体 CDN——离线必须能跑，字体一律走系统栈。
     引了 CDN 就等于把"能不能开机"押在外网上，这台操作台每天要开几十次。
  2. 代理 target 改了必须同步改 docs/接口对接说明.md——注释和代码不许只改一头。
  3. 【不】开 https / 【不】加自建 WebSocket 中间件。实时感来自轮询 + 动效编排，
     契约里写死了只用 HTTP 轮询（2–5 秒），这里加 ws 代理等于给自己开后门。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 5273,
    proxy: {
      /* mock=0 时走这里；后端未起时前端会走「渊面结冰」降级态，不会白屏 */
      '/api': {
        target: 'http://127.0.0.1:20000',
        changeOrigin: true,
      },
    },
  },
  build: {
    target: 'es2022',
    sourcemap: false,
    chunkSizeWarningLimit: 700,
  },
});
