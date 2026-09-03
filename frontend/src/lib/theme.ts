/* ================================================================
   照渊台 · 主题（总导游图）

  ▍这是什么
  ① 干什么：夜渊 / 熟宣 两套材质的切换与记忆（写 documentElement 的 data-theme）
  ② 给谁看：加主题、改默认值的人
  ③ 依赖：styles/tokens.css（两套令牌都在那里定义）

  ▍★ 为什么是 data-theme 而不是 class ★
  装置（Canvas）要用 getComputedStyle 读令牌（见 device/palette.ts），
  读的是 :root 上的自定义属性。data-theme 让"当前是哪套材质"这件事
  在 DOM 上只有一处真相，JS 与 CSS 读的是同一处。

  ▍修改铁律
  1. 切主题后必须让装置重新取色（AbyssStage 的 effect 里调 refreshPalette）。
     漏了这一步，界面换了材质、水还是旧色——那就成了"面板拼接"（见 palette.ts 说明）。
  2. 【不】用 prefers-color-scheme 覆盖用户的显式选择：他手动选过就以他为准。
  3. index.html 里内联的首屏底色必须与 tokens.css 的 --bg-0 一致（防白闪，见那份注释）。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T4）*
================================================================ */

import { useCallback, useEffect, useState } from 'react';

export type ThemeId = 'deep' | 'paper';

const KEY = 'xtmem.theme';

/** 起手主题：用户存过就用他的；没存过按系统偏好；都没有走夜渊。 */
function initial(): ThemeId {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === 'deep' || saved === 'paper') return saved;
  } catch { /* 隐私模式下 localStorage 会抛，忽略即可——不该为了记主题而崩 */ }
  if (typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: light)').matches) return 'paper';
  return 'deep';
}

/**
 * useTheme()：契约
 * 入参：无
 * 返回：[theme, toggle]
 * 副作用：改 documentElement.dataset.theme、写 localStorage
 * 谁调用：App（顶栏主题钮 + d 键）
 * 改动影响：⚠️ 装置取色依赖 data-theme，改这里要顺带确认 AbyssStage 的 refreshPalette 还接得上（铁律 1）。
 */
export function useTheme(): [ThemeId, () => void] {
  const [theme, setTheme] = useState<ThemeId>(initial);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem(KEY, theme); } catch { /* 同上，存不下就算了 */ }
  }, [theme]);

  const toggle = useCallback(() => setTheme((v) => (v === 'deep' ? 'paper' : 'deep')), []);
  return [theme, toggle];
}
