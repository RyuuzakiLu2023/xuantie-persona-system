/* ================================================================
   照渊台 · 取色（总导游图）

  ▍这是什么
  ① 干什么：把 CSS 令牌读成 Canvas 能用的颜色对象。装置的颜色跟主题同源
  ② 给谁看：改主题、加材质的人
  ③ 依赖：styles/tokens.css 的变量名

  ▍★ 为什么要读 CSS 而不是在 JS 里写死颜色 ★
  两套主题（夜渊 / 熟宣）是两种材质，不是反色。若 Canvas 里写死颜色，
  切主题时装置会和界面脱节——那就成了"面板拼接"，而不是同一件东西。
  所以：唯一颜色出处仍是 tokens.css，这里只做读取与派生。

  ▍修改铁律
  1. 【不】在本文件写十六进制字面色，除了 mix() 的中性锚点。
     要新颜色先去 tokens.css 加令牌。
  2. 主题切换后必须重新调用 readPalette()（AbyssEngine 里监听了 data-theme 变化）。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

export interface Palette {
  theme: 'deep' | 'paper';
  bg0: string; bg1: string; bg2: string; bg3: string;
  line0: string; line1: string;
  ink0: string; ink1: string; ink2: string;
  abyss: string; abyssDeep: string;
  gold: string; goldDim: string;
  amber: string; cinnabar: string;
  seal: string;
}

const v = (cs: CSSStyleDeclaration, name: string, fb: string) =>
  (cs.getPropertyValue(name) || fb).trim();

/**
 * readPalette()：契约
 * 入参：无（读 documentElement 的计算样式）
 * 返回：Palette
 * 副作用：无
 * 谁调用：AbyssEngine 构造时与主题切换时
 * 改动影响：加字段要同时在 tokens.css 两套主题里都定义，否则一套主题会拿到兜底色。
 */
export function readPalette(): Palette {
  const root = document.documentElement;
  const cs = getComputedStyle(root);
  const theme = (root.dataset.theme === 'paper' ? 'paper' : 'deep') as 'deep' | 'paper';
  return {
    theme,
    bg0: v(cs, '--bg-0', '#0b1016'),
    bg1: v(cs, '--bg-1', '#111823'),
    bg2: v(cs, '--bg-2', '#17202c'),
    bg3: v(cs, '--bg-3', '#1e2836'),
    line0: v(cs, '--line-0', '#23303f'),
    line1: v(cs, '--line-1', '#33445a'),
    ink0: v(cs, '--ink-0', '#e8e2d4'),
    ink1: v(cs, '--ink-1', '#a8a293'),
    ink2: v(cs, '--ink-2', '#8a8477'),
    abyss: v(cs, '--abyss', '#2f6d73'),
    abyssDeep: v(cs, '--abyss-deep', '#143038'),
    gold: v(cs, '--gold', '#d9a94c'),
    goldDim: v(cs, '--gold-dim', '#8a6f33'),
    amber: v(cs, '--amber', '#dd9040'),
    cinnabar: v(cs, '--cinnabar', '#e05a4a'),
    seal: v(cs, '--seal', '#0d1219'),
  };
}

/** 解析 #rgb / #rrggbb 成三元组。装置里要做透明度与混色，必须能拆开。 */
export function rgb(hex: string): [number, number, number] {
  const s = hex.replace('#', '').trim();
  if (s.length === 3) return [parseInt(s[0] + s[0], 16), parseInt(s[1] + s[1], 16), parseInt(s[2] + s[2], 16)];
  if (s.length >= 6) return [parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)];
  return [128, 128, 128];
}

/** 带透明度取色。Canvas 里大量半透明叠色靠它。 */
export function a(hex: string, alpha: number): string {
  const [r, g, b] = rgb(hex);
  return `rgba(${r},${g},${b},${Math.max(0, Math.min(1, alpha))})`;
}

/** 两色按 t 混合。用于"水越深越暗""热度越高越金"这类连续过渡。 */
export function mix(h1: string, h2: string, t: number): string {
  const [r1, g1, b1] = rgb(h1);
  const [r2, g2, b2] = rgb(h2);
  const k = Math.max(0, Math.min(1, t));
  return `rgb(${Math.round(r1 + (r2 - r1) * k)},${Math.round(g1 + (g2 - g1) * k)},${Math.round(b1 + (b2 - b1) * k)})`;
}
