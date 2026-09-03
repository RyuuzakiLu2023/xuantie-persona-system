/* ================================================================
   照渊台 · 格式化小工具（总导游图）

  ▍这是什么
  ① 干什么：相对时间、数字分组、时长的中文说法——全站统一口径
  ② 给谁看：写视图的人
  ③ 依赖：无

  ▍修改铁律
  1. 数字一律走 group()，【不】用 toLocaleString()——不同环境分隔符会漂，
     等宽对齐会因此错位（全站数字要能上下扫齐）。
  2. 相对时间【不】显示"0 秒前"这种废话，最小粒度是"刚刚"。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

/** 千分位，固定用半角逗号，保证等宽对齐稳定（见铁律 1）。 */
export function group(n: number): string {
  const s = Math.round(n).toString();
  return s.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** 相对时间。now 由 useNow() 提供，避免每帧重算。 */
export function ago(iso: string, now: number): string {
  const d = now - +new Date(iso);
  if (!Number.isFinite(d)) return '—';
  const s = Math.floor(d / 1000);
  if (s < 20) return '刚刚';
  if (s < 60) return `${s} 秒前`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  const day = Math.floor(h / 24);
  if (day < 30) return `${day} 天前`;
  return `${Math.floor(day / 30)} 月前`;
}

/** 时长的中文说法，用于唤醒延迟这类量。 */
export function dur(ms: number): string {
  if (ms <= 0) return '即时';
  if (ms < 1000) return `${ms} 毫秒`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} 秒`;
  return `${(ms / 60_000).toFixed(1)} 分`;
}

/** 百分比，一位小数以内。 */
export function pct(v: number, digits = 0): string {
  return `${(v * 100).toFixed(digits)}%`;
}

/** 钟点，用于"上次成功"这类绝对时刻。 */
export function clock(iso: string): string {
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
