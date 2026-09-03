/* ================================================================
   照渊台 · 核心装置「渊」引擎（总导游图）★ 全站最核心，改动前务必读懂 ★

  ▍这是什么
  ① 干什么：一个持续运行的 Canvas 2D 装置。它不是一次性播放的动画，
     而是有自己运动逻辑、状态与节奏的活物：水在动、灯在烧、记忆在沉浮衰减
  ② 技术栈：原生 Canvas 2D + requestAnimationFrame，零动画库
  ③ 依赖：layout.ts（几何）、palette.ts（颜色跟主题同源）、api/*（数据形状）
  ④ 给谁看：改装置行为、加新幕、调性能的人

  ▍★ 三条题材硬约束在哪落地 ★
  多主体共存 → drawLamps()：四盏灯焰型不同（常明/引火/檐下/行灯），
               灯与灯之间有暗金丝（六组两两关系），焰下有各自的记忆群
  分层       → drawWater() 两层水 + drawMemories() 的 tier 判定 +
               蒸馏时 promote 粒子把点从沉层送到浮层（两层可相互转化）
  时间       → 每颗记忆的寿命环（life）随各自 owner 的 clock_rate 走；
               到期 dissolve 成沙落向石阶；被取代则翻面成灰；
               超时的帖在水面转琥珀 —— 全部是看得见的运动，不是静态构图

  ▍文件从上到下的结构
  类型与常量      Particle / Ripple / Spark 等瞬时物件
  AbyssEngine     构造 → resize → setData → 每帧 step/draw → dispose
  绘制分层        底纹 → 两层水 → 石阶 → 记忆点 → 检索光柱 → 投物与涟漪 →
                  火星 → 四盏灯 → 灯间暗金丝 → 封蜡 → 前景纹样
  命中测试        pick(x,y) 给外壳做 hover / 键盘焦点

  ▍修改铁律（血泪教训）
  1. 【不】在 draw 里 new 对象。每帧新建数组会让 GC 在长期使用中周期性卡顿——
     这台工具每天开几十次且长时间挂着，卡顿会被立刻感知到。用预分配池。
  2. 所有随机必须来自 hash32(id)。【不】用 Math.random()：
     位置每次打开都变会砸掉空间记忆（见 layout.ts 铁律 2）。
  3. reduced-motion 下【不】停止装置，改为"离散呈现"：水面静止但分层、寿命、
     状态照旧可读（见 setReduced）。关掉动画等于关掉信息。
  4. 每个动效都要能回答"它在告诉我什么状态变了"。加动效前先把这句话写进注释；
     写不出来就【不】要加。
  5. 【不】用 shadowBlur 做大面积发光（Canvas 里它极慢）。要"亮"用叠加半透明圆。

  ▍外号表
  "小本本"   → this.seen，记录哪些记忆已经入过场（避免重复播上浮动画）
  "沙"       → dissolve 粒子，淘汰的记忆碎成沙落向石阶
  "火星"     → Spark，对端流式回传时从灯往投点落的粒子
  "暗金丝"   → bond 线，四盏灯之间的亲疏/信任/人情债
  "光柱"     → beam，检索时先扫浮层、落空才探底

  ▍已知问题
  ⚠️ TODO（2026-09-02）：记忆点上限 420 颗（MAX_NODES）。真后端若一次给全量 3214 条，
     需要在 setData 里做视口采样——现在是按 heat+life 取前 420 颗，够看但不是全量。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

import type { AgentId } from '../api/contract';
import type { Agent, Bond, MemoryNode } from '../api/endpoints';
import { hash32, layout, slotX, slotY, type Geom } from './layout';
import { a, mix, readPalette, type Palette } from './palette';

/* ---------- 常量：全部集中在此，别散落进绘制代码 ---------- */
const MAX_NODES = 420;      /* 见文件头 TODO */
const MAX_RIPPLES = 24;
const MAX_SPARKS = 260;
const MAX_SAND = 320;
const LAMP_ORDER: AgentId[] = ['xuantie', 'dsh', 'workbuddy', 'zcode'];

/* ---------- 瞬时物件（预分配，见铁律 1）---------- */

interface Ripple {
  alive: boolean;
  x: number; y: number;
  r: number; rMax: number;
  /** 0=普通涟漪 1=琥珀（超时）2=金（回声环回） */
  tone: 0 | 1 | 2;
  w: number;
}

interface Spark {
  alive: boolean;
  x: number; y: number; vx: number; vy: number; life: number;
  tone: 0 | 1; /* 0=火星（流式）1=沙（淘汰） */
}

/** 投物：一枚正在下沉的帖。sink 幕之后它会变成一颗真正的记忆点。 */
interface Stone {
  alive: boolean;
  id: string;
  x: number; y: number; vy: number;
  targetY: number;
  form: 'pebble' | 'spray' | 'anchor' | 'seal' | 'gust';
  /** 拴在水面的锚链（差帖等回执） */
  tether: boolean;
  tone: 0 | 1; /* 0=常态 1=琥珀（超时降级） */
}

/** 装置的外部输入。★ 全部来自契约字段，装置自己【不】造事实 ★ */
export interface AbyssInput {
  agents: Agent[];
  memories: MemoryNode[];
  bonds: Bond[];
  /** 检索光柱：有值时装置演一次"先问浮层，落空回落沉层" */
  probe: { q: string; hitRefined: boolean; at: number } | null;
  /** 全量/精炼条数，用于水位（库有多满） */
  vitals: { raw: number; refined: number; total: number };
  /** 活性：整台操作台的心跳，决定水面基础扰动幅度 */
  liveness: number;
  /** 被高亮的记忆 id（渊谱里选中的那条），装置里会打一圈金环 */
  focusMemoryId: string | null;
  /** 被高亮的灯 */
  focusAgent: AgentId | null;
}

/** 命中测试结果，给外壳做 hover / 键盘焦点。 */
export type Hit =
  | { kind: 'lamp'; id: AgentId; x: number; y: number }
  | { kind: 'memory'; id: string; x: number; y: number; node: MemoryNode }
  | null;

/* ================================================================
   AbyssEngine — 装置本体
   生命周期：new → attach(canvas) → setData(input) 反复 → dispose()
   ================================================================ */
export class AbyssEngine {
  private cv: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private pal: Palette;
  private g: Geom;
  private dpr = 1;
  private raf = 0;
  private t = 0;          /* 装置自己的时钟（毫秒），reduced 下按秒步进 */
  private last = 0;
  private reduced = false;
  private running = true;

  private input: AbyssInput = {
    agents: [], memories: [], bonds: [], probe: null,
    vitals: { raw: 0, refined: 0, total: 0 }, liveness: 0,
    focusMemoryId: null, focusAgent: null,
  };

  /* 节点的绘制态：位置一次算好（确定性），只有寿命/热度随数据变 */
  private nodes: {
    id: string; x: number; y: number; r: number;
    node: MemoryNode; ownerIdx: number;
    /** 上浮动画进度 0–1（沉层→浮层的转化，见三条硬约束「分层」） */
    promote: number;
    /** 翻面进度（被取代） */
    flip: number;
  }[] = [];
  /** "小本本"：记录哪些记忆已入过场，避免重复播上浮动画 */
  private seen = new Set<string>();

  private ripples: Ripple[] = Array.from({ length: MAX_RIPPLES }, () => ({ alive: false, x: 0, y: 0, r: 0, rMax: 0, tone: 0, w: 1 }));
  private sparks: Spark[] = Array.from({ length: MAX_SPARKS }, () => ({ alive: false, x: 0, y: 0, vx: 0, vy: 0, life: 0, tone: 0 }));
  private sand: Spark[] = Array.from({ length: MAX_SAND }, () => ({ alive: false, x: 0, y: 0, vx: 0, vy: 0, life: 0, tone: 1 }));
  private stones: Stone[] = Array.from({ length: 12 }, () => ({ alive: false, id: '', x: 0, y: 0, vy: 0, targetY: 0, form: 'pebble', tether: false, tone: 0 }));

  /** 灯焰相位：每盏灯自己的节奏，四盏不同步（否则看起来像一个东西的四份拷贝） */
  private lampPhase = LAMP_ORDER.map((id) => hash32(id) * Math.PI * 2);
  /** 灯的爆芯冲量：唤醒那一下 */
  private lampKick = [0, 0, 0, 0];

  /** 光柱状态：0=无，>0 为进行中的进度（先扫浮层，落空才探底） */
  private beam = { t: 0, hitRefined: false, x: 0 };

  private onHit: ((h: Hit) => void) | null = null;
  private hover: Hit = null;

  constructor(cv: HTMLCanvasElement) {
    this.cv = cv;
    const ctx = cv.getContext('2d', { alpha: false });
    if (!ctx) throw new Error('取不到 2d 上下文——这台装置没有降级到 DOM 的分支，见 AbyssPanel 的静态替代');
    this.ctx = ctx;
    this.pal = readPalette();
    this.g = layout(cv.clientWidth || 800, cv.clientHeight || 480);
    this.resize();
    this.loop = this.loop.bind(this);
    /* ⚠️ 先画一帧再排 rAF：alpha:false 的画布初值是不透明黑，
       若首帧要等 rAF（后台标签页里 rAF 被浏览器暂停）就会露出一块黑洞，
       而不是主题底色（血泪：在后台标签页打开本站时必现，见 resize 同款问题）。 */
    this.draw();
    this.raf = requestAnimationFrame(this.loop);
  }

  /** 主题切换后重新取色：装置和界面必须是同一件东西（见 palette.ts 说明）。 */
  refreshPalette() { this.pal = readPalette(); }

  /**
   * setReduced()：reduced-motion 的替代方案。
   * ⚠️ 【不】停止装置（铁律 3）。改为：水面停在中位、粒子不再飞行、
   *    上浮/翻面/溶解一步到位——分层、寿命、状态照旧全部可读。
   */
  setReduced(v: boolean) { this.reduced = v; }

  /**
   * setRunning()：页面不可见就停（省电费与风扇，见 hooks.ts 铁律 2）。
   * ⚠️ "停"= 停止推进与重绘，画面保留最后一帧（静帧）；【不】是清空画布——
   *    切回来那一眼应该还是上次那池水，而不是一块黑。
   */
  setRunning(v: boolean) {
    const was = this.running;
    this.running = v;
    if (v && !this.raf) {
      /* 停了多久都不算进装置时钟：last 清零，避免复活时 dt 巨大把粒子一次性甩飞 */
      this.last = 0;
      this.raf = requestAnimationFrame(this.loop);
    }
    if (!v && was) this.draw();
  }

  onPick(fn: (h: Hit) => void) { this.onHit = fn; }

  resize() {
    const w = this.cv.clientWidth || 800;
    const h = this.cv.clientHeight || 480;
    this.dpr = Math.min(2, window.devicePixelRatio || 1);
    this.cv.width = Math.round(w * this.dpr);
    this.cv.height = Math.round(h * this.dpr);
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    this.g = layout(w, h);
    this.relayout();
    /* ⚠️ 改 canvas.width/height 会把位图清成透明（alpha:false 下即不透明黑）。
       装置正跑时下一帧就补上了；【不】跑时（页面不可见 / reduced 停帧）必须自己补画一帧，
       否则拖窗口回来看到的是一块黑（血泪：ResizeObserver 在后台也会触发）。 */
    if (!this.running || !this.raf) this.draw();
  }

  /** 数据来了：只更新事实，不直接改画面——画面由每帧 step 追上来。 */
  setData(input: AbyssInput) {
    const prevProbe = this.input.probe?.at ?? 0;
    this.input = input;
    this.relayout();
    /* 新的检索请求 → 起一根光柱 */
    if (input.probe && input.probe.at !== prevProbe) {
      this.beam = { t: 0.0001, hitRefined: input.probe.hitRefined, x: this.g.w * (0.28 + hash32(input.probe.q) * 0.44) };
    }
    /* ⚠️ 暂停态（setRunning(false)，或 rAF 被浏览器节流）下也要把新事实画上去：
       "停"只该停运动，【不】该让画面留在旧事实上——否则切回前台的第一眼
       是过期的水位与灯态，而库主无法分辨"这是旧帧"还是"真的没变"。 */
    if (!this.running || !this.raf) this.draw();
  }

  /** 外部事件：一条帖入水。★ 这是灵魂交互与装置的接口 ★ */
  cast(opts: { id: string; to: AgentId; form: Stone['form']; depth: number; weight: number; tether: boolean }) {
    const li = LAMP_ORDER.indexOf(opts.to);
    const lamp = this.g.lamps[li < 0 ? 0 : li];
    /* 投点：库主站在中间投，落点偏向收件人那盏灯 —— "投给谁"看得出来 */
    const x = this.g.w * 0.5 + (lamp.x - this.g.w * 0.5) * 0.55;
    const s = this.stones.find((v) => !v.alive) ?? this.stones[0];
    s.alive = true; s.id = opts.id; s.x = x; s.y = this.g.surfaceY - 10;
    s.vy = 0.06 + opts.weight * 0.1;
    s.targetY = opts.form === 'gust' || opts.form === 'pebble' || opts.form === 'spray'
      ? this.g.surfaceY + 8
      : this.g.refined.top + (this.g.raw.bottom - this.g.refined.top) * opts.depth;
    s.form = opts.form; s.tether = opts.tether; s.tone = 0;
    this.spawnRipple(x, this.g.surfaceY, 26 + opts.weight * 78, 0, 1 + opts.weight);
    if (opts.form === 'spray') {
      /* 席上语：涟漪铺满全房 —— 三圈并发，说明"合席都听得到" */
      this.spawnRipple(x, this.g.surfaceY, this.g.w * 0.42, 0, 0.7);
      this.spawnRipple(x, this.g.surfaceY, this.g.w * 0.3, 0, 0.5);
    }
  }

  /** 涟漪推到灯下 → 灯爆芯。 */
  kickLamp(id: AgentId, strength = 1) {
    const i = LAMP_ORDER.indexOf(id);
    if (i >= 0) this.lampKick[i] = Math.min(2.4, this.lampKick[i] + strength);
  }

  /** 流式回传：从灯往投点落火星。tone=0 */
  emitSparks(id: AgentId, n = 6) {
    const i = LAMP_ORDER.indexOf(id);
    if (i < 0) return;
    const L = this.g.lamps[i];
    for (let k = 0; k < n; k++) {
      const s = this.sparks.find((v) => !v.alive);
      if (!s) return;
      s.alive = true;
      s.x = L.x + (hash32(id + k + this.t) - 0.5) * 10;
      s.y = L.y + 12;
      s.vx = (this.g.w * 0.5 - L.x) * 0.0006 + (hash32(id + 'v' + k) - 0.5) * 0.05;
      s.vy = 0.05 + hash32(id + 'y' + k) * 0.06;
      s.life = 1; s.tone = 0;
    }
  }

  /** 回声环回投点：金色涟漪从投点向外一圈。 */
  echo(x?: number) {
    this.spawnRipple(x ?? this.g.w * 0.5, this.g.surfaceY, 120, 2, 1.6);
  }

  /** 超时：水面那枚帖转琥珀，并起一圈琥珀涟漪。 */
  amber(id: string) {
    const s = this.stones.find((v) => v.alive && v.id === id);
    if (s) { s.tone = 1; s.targetY = this.g.surfaceY + 6; }
    this.spawnRipple(s?.x ?? this.g.w * 0.5, this.g.surfaceY, 90, 1, 1.2);
  }

  /** 淘汰：那颗记忆碎成沙落向石阶。 */
  dissolve(id: string) {
    const n = this.nodes.find((v) => v.id === id);
    if (!n) return;
    for (let k = 0; k < 14; k++) {
      const s = this.sand.find((v) => !v.alive);
      if (!s) break;
      s.alive = true;
      s.x = n.x + (hash32(id + k) - 0.5) * 8;
      s.y = n.y;
      s.vx = (hash32(id + 'sx' + k) - 0.5) * 0.03;
      s.vy = 0.02 + hash32(id + 'sy' + k) * 0.03;
      s.life = 1; s.tone = 1;
    }
  }

  dispose() {
    cancelAnimationFrame(this.raf);
    this.raf = 0;
  }

  /* ---------- 内部 ---------- */

  private spawnRipple(x: number, y: number, rMax: number, tone: Ripple['tone'], w: number) {
    const r = this.ripples.find((v) => !v.alive) ?? this.ripples[0];
    r.alive = true; r.x = x; r.y = y; r.r = 2; r.rMax = rMax; r.tone = tone; r.w = w;
  }

  /** 重算记忆点的位置（确定性，见 layout.ts 铁律 2）。 */
  private relayout() {
    const src = this.input.memories;
    /* 超量时按 heat+life 取前 MAX_NODES（见文件头 TODO） */
    const list = src.length <= MAX_NODES
      ? src
      : src.slice().sort((p, q) => (q.heat + q.life) - (p.heat + p.life)).slice(0, MAX_NODES);
    const keep = new Map(this.nodes.map((n) => [n.id, n]));
    this.nodes = list.map((m) => {
      const ownerIdx = Math.max(0, LAMP_ORDER.indexOf(m.owner));
      const old = keep.get(m.id);
      const x = slotX(this.g, m.id, ownerIdx);
      const y = slotY(this.g, m.tier, m.life, m.id);
      const r = 1.6 + m.importance * 2.1 + m.heat * 1.5;
      const first = !this.seen.has(m.id);
      this.seen.add(m.id);
      return {
        id: m.id, x, y, r, node: m, ownerIdx,
        /* 首次入场且已在浮层 → 播一次上浮；否则沿用旧进度 */
        promote: old?.promote ?? (first && m.tier === 'refined' ? 0 : 1),
        flip: old?.flip ?? (m.retired ? 1 : 0),
      };
    });
  }

  private loop(now: number) {
    if (!this.running) { this.raf = 0; return; }
    const dt = this.last ? Math.min(48, now - this.last) : 16;
    this.last = now;
    this.t += this.reduced ? 0 : dt;
    this.step(dt);
    this.draw();
    this.raf = requestAnimationFrame(this.loop);
  }

  /* ================================================================
     step — 推进物理
     ⚠️ reduced 下所有位移一步到位（铁律 3）：不再有连续运动，
     但分层、寿命、状态照旧全部可读。
     ================================================================ */
  private step(dt: number) {
    const k = this.reduced ? 999 : dt / 16;

    /* 涟漪：向外扩张并变淡。它在说"这条帖走到哪儿了" */
    for (const r of this.ripples) {
      if (!r.alive) continue;
      r.r += (r.rMax - r.r) * 0.055 * k;
      if (r.r > r.rMax - 1.2) r.alive = false;
    }

    /* 投物：下沉到目标深度，然后落进 nodes（由 setData 接手）*/
    for (const s of this.stones) {
      if (!s.alive) continue;
      if (this.reduced) { s.y = s.targetY; s.alive = s.tether || s.tone === 1; continue; }
      s.y += s.vy * dt;
      s.vy += 0.0006 * dt;          /* 水里也有重力，只是慢 */
      s.vy *= 1 - 0.0012 * dt;      /* 阻尼：越深越慢，像真的在水里 */
      if (s.y >= s.targetY) {
        s.y = s.targetY;
        /* 拴链的（差帖等回执）与琥珀的（超时留帖）留在画面上，其余交给记忆点 */
        if (!s.tether && s.tone === 0) s.alive = false;
      }
    }

    /* 火星：从灯往投点落，落到水面激起小涟漪。它在说"对端正在干活" */
    for (const s of this.sparks) {
      if (!s.alive) continue;
      if (this.reduced) { s.alive = false; continue; }
      s.x += s.vx * dt; s.y += s.vy * dt;
      s.life -= dt / 900;
      if (s.y >= this.g.surfaceY) {
        s.alive = false;
        if (hash32(String(Math.round(s.x))) > 0.55) this.spawnRipple(s.x, this.g.surfaceY, 14, 2, 0.5);
      }
      if (s.life <= 0) s.alive = false;
    }

    /* 沙：淘汰的记忆碎片落向石阶，堆在渊底。它在说"这条被时间带走了" */
    for (const s of this.sand) {
      if (!s.alive) continue;
      if (this.reduced) { s.alive = false; continue; }
      s.x += s.vx * dt; s.y += s.vy * dt;
      s.vy += 0.00004 * dt;
      s.life -= dt / 2600;
      if (s.y >= this.g.stepsY - 2 || s.life <= 0) s.alive = false;
    }

    /* 灯焰：各自节奏 + 爆芯冲量衰减 */
    for (let i = 0; i < this.lampPhase.length; i++) {
      const ag = this.input.agents.find((x) => x.id === LAMP_ORDER[i]);
      /* 焰的频率跟"能多快回话"挂钩：常明的稳、要拉起的急、行灯的近乎不动 */
      const rate = ag ? (ag.lamp === 'working' ? 0.011 : ag.lamp === 'waking' ? 0.016 : ag.lamp === 'online' ? 0.0045 : 0.0016) : 0.003;
      this.lampPhase[i] += rate * dt;
      this.lampKick[i] *= 1 - 0.0022 * dt;
      if (this.lampKick[i] < 0.002) this.lampKick[i] = 0;
    }

    /* 上浮 / 翻面：两层可相互转化（三条硬约束「分层」） */
    for (const n of this.nodes) {
      const wantPromote = n.node.tier === 'refined' ? 1 : 0;
      n.promote += (wantPromote - n.promote) * (this.reduced ? 1 : 0.02 * k);
      const wantFlip = n.node.retired ? 1 : 0;
      n.flip += (wantFlip - n.flip) * (this.reduced ? 1 : 0.03 * k);
    }

    /* 光柱：先扫浮层，落空才探底。它在说"检索是怎么找的" */
    if (this.beam.t > 0) {
      this.beam.t += (this.reduced ? 1 : dt / (this.beam.hitRefined ? 900 : 1500));
      if (this.beam.t >= 1) this.beam.t = 0;
    }
  }

  /* ================================================================
     draw — 绘制分层（顺序即层次，别重排）
     底纹 → 两层水 → 石阶 → 记忆点 → 光柱 → 投物/涟漪 → 火星/沙 →
     四盏灯 → 灯间暗金丝 → 前景纹样
     ================================================================ */
  private draw() {
    const { ctx: c, g, pal } = this;
    c.fillStyle = pal.bg0;
    c.fillRect(0, 0, g.w, g.h);

    this.drawVaultPattern();
    this.drawWater();
    this.drawSteps();
    this.drawMemories();
    this.drawBeam();
    this.drawStones();
    this.drawRipples();
    this.drawParticles();
    this.drawBonds();
    this.drawLamps();
    this.drawRim();
  }

  /** 渊壁纹样：细密的云雷回纹，只在深色主题显形（纸上会显脏）。 */
  private drawVaultPattern() {
    const { ctx: c, g, pal } = this;
    if (pal.theme === 'paper') return;
    c.save();
    c.strokeStyle = a(pal.line0, 0.5);
    c.lineWidth = 1;
    const step = 26;
    for (let x = g.padX; x < g.w - g.padX; x += step) {
      const t = hash32('v' + x);
      const h = g.rimY * (0.42 + t * 0.4);
      c.beginPath();
      c.moveTo(x, 0);
      c.lineTo(x, h);
      c.lineTo(x + step * 0.42, h);
      c.stroke();
    }
    c.restore();
  }

  /**
   * 两层水。
   * ⚠️ 这是"分层"的主载体：浮层亮而薄，沉层暗而厚，中间有一道分界的浑浊。
   * 水面的起伏幅度由 liveness 驱动 —— 活性灯不是一个数字，是水在动。
   */
  private drawWater() {
    const { ctx: c, g, pal, input } = this;
    const live = Math.max(0.06, input.liveness);

    /* 沉层：全量层。越深越暗，水位由 raw 占比给出（库有多满） */
    const rawFill = input.vitals.total ? input.vitals.raw / input.vitals.total : 0;
    const gRaw = c.createLinearGradient(0, g.raw.top, 0, g.raw.bottom);
    gRaw.addColorStop(0, a(pal.abyssDeep, 0.5 + rawFill * 0.3));
    gRaw.addColorStop(1, a(pal.abyssDeep, 0.92));
    c.fillStyle = gRaw;
    c.fillRect(g.padX, g.raw.top, g.w - g.padX * 2, g.raw.h);

    /* 浮层：精炼层。薄、亮、带一层金气 —— 它是"被照亮过"的水 */
    const gRef = c.createLinearGradient(0, g.refined.top, 0, g.refined.bottom);
    gRef.addColorStop(0, a(pal.abyss, 0.34));
    gRef.addColorStop(1, a(pal.abyss, 0.16));
    c.fillStyle = gRef;
    c.fillRect(g.padX, g.refined.top, g.w - g.padX * 2, g.refined.h);
    c.strokeStyle = a(pal.gold, 0.1);
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(g.padX, g.refined.bottom + 0.5);
    c.lineTo(g.w - g.padX, g.refined.bottom + 0.5);
    c.stroke();

    /* 两层之间的浑浊带：检索落空要穿过它才探底 */
    const midTop = g.refined.bottom;
    const midH = g.raw.top - midTop;
    const gMid = c.createLinearGradient(0, midTop, 0, g.raw.top);
    gMid.addColorStop(0, a(pal.abyss, 0.1));
    gMid.addColorStop(1, a(pal.abyssDeep, 0.5));
    c.fillStyle = gMid;
    c.fillRect(g.padX, midTop, g.w - g.padX * 2, midH);

    /* 水面：两道波，相位不同，靠 liveness 定幅 */
    c.save();
    c.beginPath();
    const amp = 1.6 + live * 4.2;
    const seg = 6;
    c.moveTo(g.padX, g.surfaceY);
    for (let x = g.padX; x <= g.w - g.padX; x += seg) {
      const y = g.surfaceY
        + Math.sin(x * 0.013 + this.t * 0.0009) * amp
        + Math.sin(x * 0.031 - this.t * 0.0014) * amp * 0.45;
      c.lineTo(x, y);
    }
    c.lineTo(g.w - g.padX, g.refined.top);
    c.lineTo(g.padX, g.refined.top);
    c.closePath();
    const gSur = c.createLinearGradient(0, g.surfaceY - amp, 0, g.refined.top);
    gSur.addColorStop(0, a(pal.abyss, 0.5));
    gSur.addColorStop(1, a(pal.abyss, 0.24));
    c.fillStyle = gSur;
    c.fill();
    /* 水面高光线：一条细金，说明"这是水面" */
    c.beginPath();
    c.moveTo(g.padX, g.surfaceY);
    for (let x = g.padX; x <= g.w - g.padX; x += seg) {
      const y = g.surfaceY
        + Math.sin(x * 0.013 + this.t * 0.0009) * amp
        + Math.sin(x * 0.031 - this.t * 0.0014) * amp * 0.45;
      c.lineTo(x, y);
    }
    c.strokeStyle = a(pal.gold, 0.26 + live * 0.2);
    c.lineWidth = 1;
    c.stroke();
    c.restore();
  }

  /** 石阶：渊底。沙堆在这里，说明"淘汰不是消失，是落回底" */
  private drawSteps() {
    const { ctx: c, g, pal } = this;
    c.fillStyle = a(pal.line0, 0.85);
    c.fillRect(g.padX, g.stepsY, g.w - g.padX * 2, g.h - g.stepsY);
    c.strokeStyle = a(pal.gold, 0.08);
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(g.padX, g.stepsY + 0.5);
    c.lineTo(g.w - g.padX, g.stepsY + 0.5);
    c.stroke();
  }

  /* ================================================================
     记忆点 — 时间与可见性都画在这一颗点上
     ★ 一颗点携带六件事：★
       层（在哪条带里）/ 热度（金的程度）/ 寿命（外圈环走了多少）/
       主人（微微向那盏灯聚拢，见 slotX）/ 可见性（封蜡）/ 是否退役（翻面成灰）
     ⚠️ 封蜡纪律：private 的点【不】画正文也【不】画摘要色，只画黑蜡 + 一道金缝。
        它看得见形、读不到字，这就是"这里有东西但不该我看"的视觉语言。
     ================================================================ */
  private drawMemories() {
    const { ctx: c, g, pal, input } = this;
    for (const n of this.nodes) {
      const m = n.node;
      /* 上浮进行中：位置在两带之间插值，能看出"从沉层被捞起来" */
      const yRaw = slotY(g, 'raw', m.life, m.id);
      const yRef = slotY(g, 'refined', m.life, m.id);
      const y = m.tier === 'refined' ? yRaw + (yRef - yRaw) * n.promote : yRaw;
      n.y = y;
      const sealed = m.visibility === 'private';
      const dim = m.retired ? 0.35 : 1;
      const r = n.r * (1 + n.promote * 0.12);

      /* 封蜡：黑蜡饼 + 一道金缝。定向共享则缝更长（"蜡上开一道缝"） */
      if (sealed) {
        c.beginPath();
        c.arc(n.x, y, r + 1.1, 0, Math.PI * 2);
        c.fillStyle = a(pal.seal, 0.96);
        c.fill();
        c.strokeStyle = a(pal.goldDim, 0.6 * dim);
        c.lineWidth = 0.9;
        c.stroke();
        c.beginPath();
        c.moveTo(n.x - r * 0.7, y);
        c.lineTo(n.x + r * 0.7, y);
        c.strokeStyle = a(pal.gold, 0.5 * dim);
        c.lineWidth = 0.9;
        c.stroke();
      } else {
        /* 热度决定金的程度：命中会回写热度，所以"常被查到的记忆是金的" */
        const body = mix(pal.abyss, pal.gold, Math.min(0.85, m.heat * 0.9));
        c.beginPath();
        c.arc(n.x, y, r, 0, Math.PI * 2);
        c.fillStyle = a(body, (0.35 + m.importance * 0.5) * dim);
        c.fill();
        if (m.visibility === 'shared_with') {
          /* 定向：半圈实线半圈虚，说明"只照给点名的那几位" */
          c.beginPath();
          c.arc(n.x, y, r + 1.6, -Math.PI * 0.35, Math.PI * 0.65);
          c.strokeStyle = a(pal.gold, 0.55 * dim);
          c.lineWidth = 0.9;
          c.stroke();
        }
      }

      /* 寿命环：外圈缺口 = 已经过去的时间。★「时间」硬约束的主载体 ★
         每盏灯的钟走得不一样快，所以同一批记忆的环长短不同。 */
      if (m.life > 0.001 && !m.retired) {
        const ag = input.agents.find((x) => x.id === m.owner);
        const rate = ag?.clock_rate ?? 1;
        const sweep = Math.PI * 2 * Math.max(0, Math.min(1, m.life));
        c.beginPath();
        c.arc(n.x, y, r + 2.6, -Math.PI / 2, -Math.PI / 2 + sweep);
        c.strokeStyle = a(m.life < 0.22 ? pal.cinnabar : pal.gold, (0.2 + m.life * 0.45) * dim);
        /* 钟走得快的灯，环画粗一点——它的记忆凉得快，值得多看一眼 */
        c.lineWidth = 0.7 + Math.min(0.9, rate * 0.5);
        c.stroke();
      }

      /* 退役：翻面成灰 + 一道横杠（被新事实取代，但没被删） */
      if (n.flip > 0.02) {
        c.save();
        c.globalAlpha = n.flip;
        c.beginPath();
        c.arc(n.x, y, r + 1, 0, Math.PI * 2);
        c.fillStyle = a(pal.ink2, 0.22);
        c.fill();
        c.beginPath();
        c.moveTo(n.x - r - 2, y - r - 2);
        c.lineTo(n.x + r + 2, y + r + 2);
        c.strokeStyle = a(pal.ink2, 0.7);
        c.lineWidth = 1;
        c.stroke();
        c.restore();
      }

      /* 被选中：金环。视图之间的状态延续靠它（渊谱选中 → 装置里也亮） */
      if (input.focusMemoryId === m.id) {
        const pulse = 1 + Math.sin(this.t * 0.006) * 0.12;
        c.beginPath();
        c.arc(n.x, y, (r + 6) * pulse, 0, Math.PI * 2);
        c.strokeStyle = a(pal.gold, 0.85);
        c.lineWidth = 1.4;
        c.stroke();
      }

      /* 聚焦某盏灯时，非它的记忆压暗 —— "谁看不见谁"的即时演示 */
      if (input.focusAgent && m.owner !== input.focusAgent) {
        c.beginPath();
        c.arc(n.x, y, r + 3, 0, Math.PI * 2);
        c.fillStyle = a(pal.bg0, 0.55);
        c.fill();
      }
    }
  }

  /* ================================================================
     检索光柱 — "先问精炼层，落空才回落全量"
     命中浮层：光柱到浮层就停，命中的点亮一下（热度回写）
     落空：光柱穿过浑浊带继续探底，行程更长（所以落空是"更慢"的，看得出来）
     ================================================================ */
  private drawBeam() {
    if (this.beam.t <= 0) return;
    const { ctx: c, g, pal } = this;
    const p = Math.min(1, this.beam.t);
    const from = g.surfaceY;
    const stopRefined = g.refined.bottom;
    const stopRaw = g.raw.bottom - 6;
    const end = this.beam.hitRefined
      ? from + (stopRefined - from) * p
      : from + (stopRaw - from) * p;
    const x = this.beam.x;

    c.save();
    const grad = c.createLinearGradient(x, from, x, end);
    grad.addColorStop(0, a(pal.gold, 0.42));
    grad.addColorStop(1, a(pal.gold, 0.02));
    c.strokeStyle = grad;
    c.lineWidth = 2.2;
    c.beginPath();
    c.moveTo(x, from);
    c.lineTo(x, end);
    c.stroke();

    /* 柱头：一枚小亮点在往下探 */
    c.beginPath();
    c.arc(x, end, 2.4, 0, Math.PI * 2);
    c.fillStyle = a(pal.gold, 0.9);
    c.fill();

    /* 落空的柱穿过浑浊带时抖一下：说明"这一段是额外代价" */
    if (!this.beam.hitRefined && end > stopRefined && end < g.raw.top) {
      c.beginPath();
      c.arc(x, end, 6, 0, Math.PI * 2);
      c.strokeStyle = a(pal.cinnabar, 0.35);
      c.lineWidth = 1;
      c.stroke();
    }
    c.restore();
  }

  /** 投物与锚链。差帖的锚链一直拴到水面，直到回执才解开。 */
  private drawStones() {
    const { ctx: c, g, pal } = this;
    for (const s of this.stones) {
      if (!s.alive) continue;
      const tone = s.tone === 1 ? pal.amber : pal.gold;

      if (s.tether) {
        /* 锚链：一条微微摆动的线，从水面拴到锚。它在说"这活还没了结" */
        c.beginPath();
        c.moveTo(s.x, g.surfaceY);
        const segs = 6;
        for (let i = 1; i <= segs; i++) {
          const t = i / segs;
          const sway = Math.sin(this.t * 0.0022 + t * 3) * 3 * (1 - t);
          c.lineTo(s.x + sway, g.surfaceY + (s.y - g.surfaceY) * t);
        }
        c.strokeStyle = a(tone, 0.4);
        c.lineWidth = 1;
        c.stroke();
      }

      c.save();
      c.translate(s.x, s.y);
      c.fillStyle = a(tone, 0.9);
      c.strokeStyle = a(tone, 0.9);
      c.lineWidth = 1.2;
      switch (s.form) {
        case 'anchor': {
          /* 铁锚：一竖一横一钩 */
          c.beginPath(); c.moveTo(0, -6); c.lineTo(0, 6); c.stroke();
          c.beginPath(); c.moveTo(-4, -3); c.lineTo(4, -3); c.stroke();
          c.beginPath(); c.arc(0, 5, 4.5, 0.15 * Math.PI, 0.85 * Math.PI); c.stroke();
          break;
        }
        case 'seal': {
          /* 石印：一方小印，落地即镀金 */
          c.beginPath(); c.rect(-4.5, -4.5, 9, 9); c.fill();
          c.strokeStyle = a(pal.bg0, 0.8);
          c.beginPath(); c.rect(-2, -2, 4, 4); c.stroke();
          break;
        }
        case 'gust': {
          /* 风讯：一道弧痕，不入水 */
          c.beginPath(); c.arc(0, 0, 7, -0.2 * Math.PI, 0.5 * Math.PI); c.stroke();
          break;
        }
        case 'spray': {
          for (let i = 0; i < 5; i++) {
            const ang = (i / 5) * Math.PI * 2 + hash32(s.id) * 6;
            c.beginPath();
            c.arc(Math.cos(ang) * 4, Math.sin(ang) * 4, 1.4, 0, Math.PI * 2);
            c.fill();
          }
          break;
        }
        default: {
          c.beginPath(); c.arc(0, 0, 3.2, 0, Math.PI * 2); c.fill();
        }
      }
      c.restore();
    }
  }

  private drawRipples() {
    const { ctx: c, pal } = this;
    for (const r of this.ripples) {
      if (!r.alive) continue;
      const fade = 1 - r.r / r.rMax;
      const col = r.tone === 1 ? pal.amber : r.tone === 2 ? pal.gold : pal.abyss;
      c.beginPath();
      /* 椭圆：水面是有透视的，圆会显得像贴在玻璃上 */
      c.ellipse(r.x, r.y, r.r, r.r * 0.26, 0, 0, Math.PI * 2);
      c.strokeStyle = a(col, 0.5 * fade);
      c.lineWidth = r.w;
      c.stroke();
    }
  }

  private drawParticles() {
    const { ctx: c, pal } = this;
    for (const s of this.sparks) {
      if (!s.alive) continue;
      c.beginPath();
      c.arc(s.x, s.y, 1.3, 0, Math.PI * 2);
      c.fillStyle = a(pal.gold, 0.8 * s.life);
      c.fill();
    }
    for (const s of this.sand) {
      if (!s.alive) continue;
      c.beginPath();
      c.arc(s.x, s.y, 1, 0, Math.PI * 2);
      c.fillStyle = a(pal.ink2, 0.55 * s.life);
      c.fill();
    }
  }

  /* ================================================================
     灯间暗金丝 — 六组两两关系（他们之间有关系，不是四个 API 端点）
     粗细 = 亲疏 affinity；亮度 = 信任 trust；
     游走的小点 = 人情债 favor_debt，正数从 a 流向 b（谁欠谁看得出来）
     ⚠️ 丝画在灯的下方、水的上方：它连的是"人格"，【不】穿过水面——
        因为记忆是水全楼通，人格是房各自关门。丝走空中不走水里。
     ================================================================ */
  private drawBonds() {
    const { ctx: c, g, pal, input } = this;
    if (!input.bonds.length) return;
    for (const b of input.bonds) {
      const ia = LAMP_ORDER.indexOf(b.a);
      const ib = LAMP_ORDER.indexOf(b.b);
      if (ia < 0 || ib < 0) continue;
      const A = g.lamps[ia], B = g.lamps[ib];
      /* 弧线拱起：距离越远拱得越高，免得四条丝叠成一团 */
      const lift = 10 + Math.abs(ia - ib) * 13;
      const mx = (A.x + B.x) / 2;
      const my = Math.max(6, A.y - lift);

      const focus = input.focusAgent;
      const related = !focus || focus === b.a || focus === b.b;
      c.save();
      c.beginPath();
      c.moveTo(A.x, A.y - 6);
      c.quadraticCurveTo(mx, my, B.x, B.y - 6);
      c.strokeStyle = a(pal.goldDim, (0.1 + b.trust * 0.3) * (related ? 1 : 0.22));
      c.lineWidth = 0.5 + b.affinity * 1.7;
      c.stroke();

      /* 人情债：一颗小点在丝上走，方向由 favor_debt 正负决定 */
      if (b.favor_debt !== 0 && related) {
        const speed = 0.00016 * (1 + Math.abs(b.favor_debt) * 0.4);
        let t = ((this.t * speed) % 1 + 1) % 1;
        if (b.favor_debt < 0) t = 1 - t;
        const px = (1 - t) * (1 - t) * A.x + 2 * (1 - t) * t * mx + t * t * B.x;
        const py = (1 - t) * (1 - t) * (A.y - 6) + 2 * (1 - t) * t * my + t * t * (B.y - 6);
        c.beginPath();
        c.arc(px, py, 1.6, 0, Math.PI * 2);
        c.fillStyle = a(pal.gold, 0.75);
        c.fill();
      }
      c.restore();
    }
  }

  /* ================================================================
     四盏灯 — ★「多主体共存」硬约束的主载体 ★
     四种焰型对应四种在线方式，不是四个头像：
       常明 resident  一柱稳焰，微微呼吸
       引火 on_demand 焰短而抖，底下有一点余烬（要拉起来）
       檐下 desktop   焰被风吹得偏斜（人不在机器前时更斜）
       行灯 session   只剩烬火，一圈虚线灯罩（会话关了）
     另外三件事画在灯上：
       未读        → 焰高（【不】用红点，见设计说明"不做"第 1 条）
       爆芯        → lampKick，唤醒那一下
       照不到      → focusAgent 时其他灯灰掉（谁看不见谁）
     ================================================================ */
  private drawLamps() {
    const { ctx: c, g, pal, input } = this;
    for (let i = 0; i < LAMP_ORDER.length; i++) {
      const id = LAMP_ORDER[i];
      const ag = input.agents.find((x) => x.id === id);
      const L = g.lamps[i];
      const dim = input.focusAgent && input.focusAgent !== id ? 0.3 : 1;
      const lamp = ag?.lamp ?? 'asleep';
      const unread = ag?.unread ?? 0;
      const kick = this.lampKick[i];

      /* 灯柱：一根细杆立在渊沿，杆头一只灯碗 */
      c.save();
      c.globalAlpha = dim;
      c.strokeStyle = a(pal.line1, 0.9);
      c.lineWidth = 1.2;
      c.beginPath();
      c.moveTo(L.x, L.y + 26);
      c.lineTo(L.x, L.y + 6);
      c.stroke();
      /* 灯碗 */
      c.beginPath();
      c.moveTo(L.x - 7, L.y + 6);
      c.quadraticCurveTo(L.x, L.y + 11, L.x + 7, L.y + 6);
      c.strokeStyle = a(pal.goldDim, 0.95);
      c.lineWidth = 1.4;
      c.stroke();

      /* 焰：四种形态。ph 是各自相位（四盏不同步，见 lampPhase） */
      const ph = this.lampPhase[i];
      const breathe = Math.sin(ph);
      /* 焰高：基础 + 未读 + 爆芯冲量。未读越多焰越高，这就是"未读"的全部呈现 */
      const baseH = lamp === 'unreachable' ? 3 : lamp === 'asleep' ? 4 : 9;
      const h = baseH + Math.min(9, unread * 1.6) + kick * 9 + breathe * (lamp === 'working' ? 3.2 : 1.1);
      const lean = lamp === 'unreachable' ? 0
        : ag?.presence === 'desktop' ? Math.sin(ph * 0.6) * 3.4   /* 檐下：被风吹得偏斜 */
        : ag?.presence === 'on_demand' ? Math.sin(ph * 1.8) * 1.6 /* 引火：短而抖 */
        : Math.sin(ph * 0.5) * 0.6;                               /* 常明：几乎不动 */

      if (lamp !== 'unreachable') {
        c.beginPath();
        c.moveTo(L.x - 3.4, L.y + 6);
        c.quadraticCurveTo(L.x + lean - 1, L.y + 6 - h * 0.6, L.x + lean, L.y + 6 - h);
        c.quadraticCurveTo(L.x + lean + 1, L.y + 6 - h * 0.6, L.x + 3.4, L.y + 6);
        const gf = c.createLinearGradient(L.x, L.y + 6, L.x, L.y + 6 - h);
        gf.addColorStop(0, a(pal.gold, 0.9));
        gf.addColorStop(1, a(pal.amber, 0.15));
        c.fillStyle = gf;
        c.fill();
        /* 焰心：一小点白金，工作时更亮 */
        c.beginPath();
        c.arc(L.x + lean * 0.4, L.y + 3 - h * 0.28, 1.3 + kick * 1.2, 0, Math.PI * 2);
        c.fillStyle = a(pal.gold, lamp === 'working' ? 1 : 0.7);
        c.fill();
      } else {
        /* 行灯灭了：只剩烬，外加一圈虚线灯罩，说明"会话关了，只能留帖" */
        c.beginPath();
        c.arc(L.x, L.y + 3, 2, 0, Math.PI * 2);
        c.fillStyle = a(pal.cinnabar, 0.5);
        c.fill();
        c.save();
        c.setLineDash([2, 3]);
        c.beginPath();
        c.arc(L.x, L.y + 1, 10, 0, Math.PI * 2);
        c.strokeStyle = a(pal.ink2, 0.4);
        c.lineWidth = 1;
        c.stroke();
        c.restore();
      }

      /* 引火型的余烬：底下一点微红，说明"它睡着但能被叫起来" */
      if (ag?.presence === 'on_demand' && lamp !== 'working') {
        c.beginPath();
        c.arc(L.x, L.y + 8.5, 1.6 + Math.sin(ph * 2) * 0.4, 0, Math.PI * 2);
        c.fillStyle = a(pal.amber, 0.5);
        c.fill();
      }

      /* 焰在水面的倒影：灯只照水，不照彼此 —— 这是概念的核心一笔 */
      if (lamp !== 'unreachable') {
        const refl = c.createLinearGradient(L.x, g.surfaceY, L.x, g.refined.top);
        refl.addColorStop(0, a(pal.gold, 0.16 + Math.min(0.16, unread * 0.02)));
        refl.addColorStop(1, a(pal.gold, 0));
        c.fillStyle = refl;
        c.beginPath();
        c.moveTo(L.x - 3 - h * 0.25, g.surfaceY);
        c.lineTo(L.x + 3 + h * 0.25, g.surfaceY);
        c.lineTo(L.x + 1.2, g.refined.top);
        c.lineTo(L.x - 1.2, g.refined.top);
        c.closePath();
        c.fill();
      }

      /* 悬停/焦点：灯座外一圈方框（键盘也能落到这里） */
      if (this.hover?.kind === 'lamp' && this.hover.id === id) {
        c.strokeStyle = a(pal.gold, 0.7);
        c.lineWidth = 1;
        c.strokeRect(L.x - 14, L.y - 14, 28, 44);
      }
      c.restore();
    }
  }

  /** 渊沿：一道横线 + 两端的回纹收头，把"这是个容器"说清楚。 */
  private drawRim() {
    const { ctx: c, g, pal } = this;
    c.strokeStyle = a(pal.line1, 0.8);
    c.lineWidth = 1;
    c.beginPath();
    c.moveTo(g.padX, g.rimY + 26.5);
    c.lineTo(g.w - g.padX, g.rimY + 26.5);
    c.stroke();
    for (const x of [g.padX, g.w - g.padX]) {
      const dir = x === g.padX ? 1 : -1;
      c.beginPath();
      c.moveTo(x, g.rimY + 26.5);
      c.lineTo(x, g.rimY + 16);
      c.lineTo(x + 9 * dir, g.rimY + 16);
      c.strokeStyle = a(pal.goldDim, 0.7);
      c.stroke();
    }
  }

  /* ================================================================
     命中测试 — 给外壳做 hover / 点击 / 键盘焦点
     ⚠️ 先测灯（灯小但更重要），再测记忆点；半径放宽 6px 让触屏也点得到。
     ================================================================ */
  pick(x: number, y: number): Hit {
    const { g } = this;
    for (let i = 0; i < g.lamps.length; i++) {
      const L = g.lamps[i];
      if (Math.abs(x - L.x) < 16 && y > L.y - 18 && y < L.y + 32) {
        return { kind: 'lamp', id: LAMP_ORDER[i], x: L.x, y: L.y };
      }
    }
    let best: Hit = null;
    let bestD = 14;
    for (const n of this.nodes) {
      const d = Math.hypot(n.x - x, n.y - y);
      if (d < Math.max(6, n.r + 6) && d < bestD) {
        bestD = d;
        best = { kind: 'memory', id: n.id, x: n.x, y: n.y, node: n.node };
      }
    }
    return best;
  }

  setHover(h: Hit) {
    this.hover = h;
    if (h && this.onHit) this.onHit(h);
  }

  /** 给外壳读几何：抽屉要对着灯下弹出，得知道灯在哪。 */
  lampPoint(id: AgentId): { x: number; y: number } {
    const i = Math.max(0, LAMP_ORDER.indexOf(id));
    return this.g.lamps[i];
  }
  get geom(): Geom { return this.g; }

  /**
   * nodePoints()：把记忆点的当前坐标交给覆盖层（渊谱的实体连线要接到水里的点上）。
   * ⚠️ 返回的是同一个 Map 实例的新副本；【不】把内部数组直接交出去，
   *    否则覆盖层一改就会把装置的布局搅乱。
   */
  nodePoints(): Map<string, { x: number; y: number; r: number; tier: 'raw' | 'refined'; owner: AgentId }> {
    const m = new Map<string, { x: number; y: number; r: number; tier: 'raw' | 'refined'; owner: AgentId }>();
    for (const n of this.nodes) {
      m.set(n.id, { x: n.x, y: n.y, r: n.r, tier: n.node.tier, owner: n.node.owner });
    }
    return m;
  }

  /**
   * 「静水」深层信息层（需要主动长按才会发现）。
   * ⚠️ 它【不】是把水停住看着好玩：停住是为了让每颗点旁边的刻度可读——
   *    连续运动下读不了小字。所以这一层是"为了读数而停"，不是装饰。
   */
  setStill(v: boolean) { this.still = v; }
  get isStill() { return this.still; }
  private still = false;
}
