/* ================================================================
   照渊台 · 纹样与图标（总导游图）

  ▍这是什么
  ① 干什么：内联 SVG 纹样与图标。★ 全站零 Emoji ★（不可协商的底线）
  ② 给谁看：加图标的人
  ③ 依赖：无（纯 SVG，颜色走 currentColor 跟随主题）

  ▍★ 为什么自己描而不用图标库 ★
  中式回纹、缠枝、水波这类纹样图标库里没有；而"华丽感靠纹样与层次"是本作的
  视觉策略（见 docs/设计说明.md 原则 3）。图标一律 currentColor + 1px 描边，
  这样在两套主题里都成立，也不必维护两份资源。

  ▍修改铁律
  1. 【不】用 Emoji，一个也不许（视觉容器内零 Emoji 是硬底线）。
  2. 新图标必须 24×24 视框、stroke=currentColor、不写死颜色。
  3. 纯装饰的 SVG 必须 aria-hidden，否则读屏器会念一堆无意义的图形。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

import type { CSSProperties } from 'react';

interface IconProps { size?: number; style?: CSSProperties; title?: string }

const box = (size: number) => ({
  width: size, height: size, viewBox: '0 0 24 24',
  fill: 'none', stroke: 'currentColor',
  strokeWidth: 1.4, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const,
});

/** 灯：四盏灯的通用符号（灯碗 + 焰）。 */
export const IconLamp = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <path d="M12 3c1.9 2.4 2.8 4 2.8 5.4 0 1.7-1.3 2.8-2.8 2.8s-2.8-1.1-2.8-2.8C9.2 7 10.1 5.4 12 3Z" />
    <path d="M6.5 13h11l-1.6 2.6H8.1L6.5 13Z" />
    <path d="M12 15.6V21" />
  </svg>
);

/** 帖：一张纸，用于「递帖」。 */
export const IconSheet = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <path d="M6 3.5h9l3.5 3.5v13.5H6z" />
    <path d="M15 3.5V7h3.5" />
    <path d="M8.8 11h6.4M8.8 14h6.4M8.8 17h3.6" />
  </svg>
);

/** 锚：差帖（要回执的活）。 */
export const IconAnchor = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <circle cx="12" cy="5" r="2.2" />
    <path d="M12 7.2V20" />
    <path d="M7.5 10.5h9" />
    <path d="M4.5 15.5c0 3 3.4 4.8 7.5 4.8s7.5-1.8 7.5-4.8" />
  </svg>
);

/** 水波：渊谱。 */
export const IconWave = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <path d="M3 8.5c2-2 4-2 6 0s4 2 6 0 4-2 6 0" />
    <path d="M3 13c2-2 4-2 6 0s4 2 6 0 4-2 6 0" />
    <path d="M3 17.5c2-2 4-2 6 0s4 2 6 0 4-2 6 0" />
  </svg>
);

/** 印：凭帖（决策归档）。 */
export const IconSeal = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <rect x="4.5" y="4.5" width="15" height="15" rx="1.5" />
    <rect x="8.5" y="8.5" width="7" height="7" />
  </svg>
);

/** 锁蜡：私密。 */
export const IconWax = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <circle cx="12" cy="12" r="6.5" />
    <path d="M8.4 12h7.2" />
    <path d="M12 5.5V3M12 21v-2.5M5.5 12H3M21 12h-2.5" />
  </svg>
);

/** 三人：群聊房间。 */
export const IconRoom = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <circle cx="8.5" cy="9" r="2.4" />
    <circle cx="15.5" cy="9" r="2.4" />
    <path d="M4 18c0-2.4 2-3.9 4.5-3.9S13 15.6 13 18" />
    <path d="M11.5 18c0-2.4 1.7-3.9 4-3.9S20 15.6 20 18" />
  </svg>
);

/** 风：系统风讯。 */
export const IconGust = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <path d="M3 8.5h11a2.5 2.5 0 1 0-2.5-2.5" />
    <path d="M3 13h14a2.5 2.5 0 1 1-2.5 2.5" />
    <path d="M3 17.5h7" />
  </svg>
);

/** 沙漏：时间与衰减。 */
export const IconGlass = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <path d="M7 3.5h10M7 20.5h10" />
    <path d="M8 3.5c0 4 4 5.2 4 8.5s-4 4.5-4 8.5" />
    <path d="M16 3.5c0 4-4 5.2-4 8.5s4 4.5 4 8.5" />
  </svg>
);

/** 日月：主题切换。用同一个符号的两半，避免出现两个图标。 */
export const IconDayNight = ({ size = 16, style, title }: IconProps) => (
  <svg {...box(size)} style={style} aria-hidden={!title} role={title ? 'img' : undefined}>
    {title && <title>{title}</title>}
    <circle cx="12" cy="12" r="6.5" />
    <path d="M12 5.5a6.5 6.5 0 0 0 0 13z" fill="currentColor" stroke="none" />
  </svg>
);

/* ================================================================
   缠枝纹：大面积底纹用，作为背景铺陈
   ⚠️ 它是纯装饰，必须 aria-hidden；透明度压到很低，否则会抢正文。
   ================================================================ */
export const VinePattern = ({ style }: { style?: CSSProperties }) => (
  <svg aria-hidden style={style} width="100%" height="100%" preserveAspectRatio="none">
    <defs>
      <pattern id="vine" width="72" height="72" patternUnits="userSpaceOnUse">
        <path d="M0 60c14-2 22-10 24-24 2-14 10-22 24-24 14-2 22-10 24-24"
          fill="none" stroke="currentColor" strokeWidth="1" opacity=".5" />
        <circle cx="24" cy="36" r="2.2" fill="none" stroke="currentColor" strokeWidth="1" opacity=".4" />
        <circle cx="48" cy="12" r="2.2" fill="none" stroke="currentColor" strokeWidth="1" opacity=".4" />
      </pattern>
    </defs>
    <rect width="100%" height="100%" fill="url(#vine)" />
  </svg>
);
