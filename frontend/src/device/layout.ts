/* ================================================================
   照渊台 · 核心装置几何（总导游图）

  ▍这是什么
  ① 干什么：把「渊」的断面几何算出来——水面在哪、两层水在哪、石阶在哪、四盏灯站在哪
  ② 给谁看：改装置构图、调层高比例的人
  ③ 依赖：无（纯计算，不碰 DOM）

  ▍★ 为什么是断面 ★
  渊必须同时给出"分层"与"时间"。俯视看不到分层，所以取纵向断面：
  水面一条线在上，精炼层悬在中，全量层沉在下，石阶垫底。
  四盏灯立在渊沿（水面之上），只照水、不照彼此——这就是"记忆是水，人格是房"。

  ▍结构
  Geom          一次算好的所有关键线与带
  layout()      由画布尺寸算 Geom（响应式：窄屏压缩灯距、抬高水面）
  slotX()/hash  确定性横坐标 —— ⚠️ 同一条记忆每次打开都在同一位置（见铁律 2）

  ▍修改铁律
  1. 【不】把比例散落到绘制代码里。所有 0.24 / 0.52 这类数字只允许出现在本文件，
     否则改一次构图要满文件找魔数。
  2. 记忆的横坐标必须是 id 的确定性函数。【不】用随机、【不】用力导向——
     每次打开位置都不同会砸掉空间记忆，而空间记忆正是长期使用变顺手的来源。
  3. 改带高必须同步 docs/设计说明.md 的构图描述（注释与代码同批改）。

  ▍外号表
  "水面"    → surfaceY，帖入水的那条线
  "浮层"    → refinedBand，精炼层所在的带
  "沉层"    → rawBand，全量层所在的带
  "石阶"    → stepsY，渊底，淘汰的沙落在这里
  "灯位"    → lampAt(i)，四盏灯的站位

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

export interface Band { top: number; bottom: number; get h(): number }

export interface Geom {
  w: number; h: number;
  /** 渊沿（灯站的那条水平线） */
  rimY: number;
  /** 水面 */
  surfaceY: number;
  /** 浮层：精炼层 */
  refined: Band;
  /** 沉层：全量层 */
  raw: Band;
  /** 石阶（渊底） */
  stepsY: number;
  /** 渊壁内缩 */
  padX: number;
  /** 四盏灯的站位 */
  lamps: { x: number; y: number }[];
  /** 窄屏形态 */
  narrow: boolean;
}

const band = (top: number, bottom: number): Band => ({
  top, bottom, get h() { return bottom - top; },
});

/**
 * layout()：契约
 * 入参：w / h（画布 CSS 像素尺寸）
 * 返回：Geom
 * 副作用：无
 * 谁调用：AbyssCanvas 的每帧绘制与命中测试
 * 改动影响：⚠️ 比例是构图的全部，改动会同时影响四灯站位、投物落点与检索光柱行程。
 */
export function layout(w: number, h: number): Geom {
  const narrow = w < 620;
  const padX = narrow ? w * 0.06 : w * 0.045;
  const rimY = h * (narrow ? 0.14 : 0.155);
  const surfaceY = h * (narrow ? 0.30 : 0.275);
  const refined = band(surfaceY + h * 0.045, h * (narrow ? 0.545 : 0.525));
  const raw = band(h * (narrow ? 0.575 : 0.555), h * 0.885);
  const stepsY = h * 0.925;

  /* 四盏灯：沿渊沿等分，窄屏收紧到中间三分之二，免得贴边被裁 */
  const n = 4;
  const spanL = narrow ? w * 0.13 : padX + w * 0.055;
  const spanR = narrow ? w * 0.87 : w - padX - w * 0.055;
  const lamps = Array.from({ length: n }, (_, i) => ({
    x: spanL + ((spanR - spanL) * i) / (n - 1),
    y: rimY,
  }));

  return { w, h, rimY, surfaceY, refined, raw, stepsY, padX, lamps, narrow };
}

/** 32 位确定性散列：同一 id 永远得到同一个数（见铁律 2）。 */
export function hash32(s: string): number {
  let x = 2166136261;
  for (let i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 16777619); }
  return (x >>> 0) / 4294967296;
}

/** 记忆的横坐标：由 id 定，稳定不漂。owner 只做轻微偏置，让同一盏灯的记忆略成群。 */
export function slotX(g: Geom, id: string, ownerIndex: number): number {
  const inner = g.w - g.padX * 2 - 24;
  const jitter = hash32(id);
  const cluster = (ownerIndex + 0.5) / 4;
  /* 七成看自己的散列（保证分散且稳定），三成向本盏灯下聚拢（保证"谁的记忆"看得出来） */
  const t = jitter * 0.7 + cluster * 0.3;
  return g.padX + 12 + t * inner;
}

/** 浮层/沉层内的纵坐标：层决定带，寿命决定在带内的高低（寿命越短越往下坠）。 */
export function slotY(g: Geom, tier: 'raw' | 'refined', life: number, id: string): number {
  const b = tier === 'refined' ? g.refined : g.raw;
  const drift = hash32(id + '#y');
  const inBand = 0.16 + drift * 0.68;
  /* 寿命把它往带底压：life=1 在带上沿，life=0 贴带底 */
  const sag = (1 - Math.max(0, Math.min(1, life))) * 0.28;
  return b.top + b.h * Math.min(0.96, inBand + sag);
}
