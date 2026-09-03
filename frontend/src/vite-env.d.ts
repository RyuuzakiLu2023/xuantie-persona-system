/* ================================================================
   照渊台 · 环境类型声明

  ▍这是什么
  ① 干什么：给 import.meta.env 上的自定义变量补类型（Vite 不会自动推出来）
  ② 给谁看：加环境变量的人 —— 加一个变量，这里也要加一行
  ③ 依赖：vite/client

  ▍修改铁律
  1. 变量名必须以 VITE_ 开头，否则不会进包（见 .env.example 铁律 1）。
  2. 这里加了字段，.env.example 也要同步加说明——只改一头，下一个人就得猜。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 'true'（默认）走 fixtures 假数据；'false' 打真后端 */
  readonly VITE_USE_MOCK?: string;
  /** 真后端根地址，留空走同源 /api */
  readonly VITE_API_BASE?: string;
  /** 轮询间隔毫秒，夹紧在 2000–5000（契约写死 2–5 秒） */
  readonly VITE_POLL_MS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
