/* ================================================================
   照渊台 · 渊外壳 AbyssStage（总导游图）★ 装置与 React 的唯一接缝 ★

  ▍这是什么
  ① 干什么：把 device/engine.ts（自跑的 Canvas 装置）接进 React：
     尺寸、主题、数据、reduced-motion、页面可见性、指针与键盘，全在这一层对接
  ② 给谁看：改装置接入方式、加覆盖层（渊谱）、改 hover 浮层的人
  ③ 依赖：device/engine.ts（装置）、lib/hooks.ts（reduced/narrow）、api/*（数据形状）

  ▍★ 为什么只有一个装置实例 ★
  四个视图共用同一个 AbyssStage 实例（App 里只挂一次）。切视图只改它的
  尺寸与周围面板，【不】卸载重建——否则水会"重开一次"，投出去那枚帖在渊里的
  位置、上浮进度、涟漪全部丢掉，视图之间的因果就断了（见 docs/设计说明.md 原则 6）。

  ▍文件从上到下的结构
  Props          外壳的输入（全部来自契约数据，装置自己不造事实）
  AbyssStage     六个 effect：建装置 / 尺寸 / 主题 / 数据 / reduced / 可见性
  指针           pointermove → pick → setHover → 浮层；pointerdown → 选中
  键盘           渊本身可 Tab 聚焦；左右方向键在记忆点之间走，Enter 选中
  覆盖层         overlay(points, geom) 渲染 SVG 叠层（渊谱用），按 100ms 采样

  ▍★ hover 浮层的封蜡纪律 ★
  私密条目浮层只写"封"与条数，【不】显示正文，也【不】写"您无权查看"——
  后端本就不下发正文（content=null），所以这里连"藏"的分支都不写：
  没有数据可藏。见 docs/设计说明.md 原则 5。

  ▍修改铁律
  1. 【不】在 render 里调 engine 的绘制方法。React 只喂数据（setData），
     画面由装置自己的 rAF 追上来——两套时钟混在一起必然打架。
  2. 覆盖层采样【不】跟 60fps 对齐（每帧 setState 会把主线程吃干）。
     100ms 一次足够：连线是"接到哪颗点"，不是逐帧动画。
  3. 页面不可见必须 setRunning(false)（省电费与风扇），可见了再开回来。
  4. 装置的 dispose 必须在 effect cleanup 里调。StrictMode 会拆装两次，
     漏一次就是两个 rAF 同时画同一张画布（症状：动效速度翻倍）。

  ▍外号表
  "渊"      → 这块画布
  "浮层"    → hover/焦点时那张一句话小卡（.abyss-tip）
  "盖层"    → 空态/错误态盖在渊上的话（.abyss-veil）
  "叠层"    → 渊谱的 SVG（overlay prop）

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T1）*
================================================================ */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { AgentId } from '../api/contract';
import type { Agent, Bond, MemoryNode } from '../api/endpoints';
import { AbyssEngine, type AbyssInput, type Hit } from '../device/engine';
import type { Geom } from '../device/layout';
import { usePrefersReducedMotion } from '../lib/hooks';
import { pct } from '../lib/format';

/** 覆盖层拿到的东西：记忆点当下坐标 + 渊的几何。 */
export interface OverlayArgs {
  points: Map<string, { x: number; y: number; r: number; tier: 'raw' | 'refined'; owner: AgentId }>;
  geom: Geom;
}

export interface AbyssStageProps {
  agents: Agent[];
  memories: MemoryNode[];
  bonds: Bond[];
  vitals: { raw: number; refined: number; total: number };
  liveness: number;
  probe: { q: string; hitRefined: boolean; at: number } | null;
  focusMemoryId: string | null;
  focusAgent: AgentId | null;
  /** ★ 把装置实例交给 App：命途推进时要调 cast/kickLamp/emitSparks/echo/amber ★ */
  engineRef: { current: AbyssEngine | null };
  onPickMemory?: (id: string | null) => void;
  onPickLamp?: (id: AgentId) => void;
  /** 渊谱的 SVG 叠层。给函数而不是元素，因为它要读装置当下的坐标 */
  overlay?: (args: OverlayArgs) => ReactNode;
  /** 空态/错误态盖层 */
  veil?: ReactNode;
  /**
   * 渊面注解：贴在渊左上角的一两行小字。
   * ⚠️ 用途是"这池水为什么缺东西"。降级时（比如图谱这条腿断了）渊里一颗记忆点都没有，
   *    若不在渊上说明，库主看到的就是"一池空水"而无从判断是真空还是读不到
   *    —— 那正是 spec 禁止的"降级=白屏"（见 错题本.md #5）。
   */
  notes?: ReactNode;
  /** 主题变了就重新取色（值本身不用，变化才是信号） */
  theme: string;
}

export function AbyssStage(props: AbyssStageProps) {
  const {
    agents, memories, bonds, vitals, liveness, probe,
    focusMemoryId, focusAgent, engineRef, onPickMemory, onPickLamp, overlay, veil, notes, theme,
  } = props;

  const hostRef = useRef<HTMLDivElement | null>(null);
  const cvRef = useRef<HTMLCanvasElement | null>(null);
  const [tip, setTip] = useState<{ x: number; y: number; hit: Hit } | null>(null);
  const [focused, setFocused] = useState(false);
  /** 覆盖层采样计数（见铁律 2）：只有挂了 overlay 才跑 */
  const [sample, setSample] = useState(0);
  const reduced = usePrefersReducedMotion();

  /* ---------- 建装置：只建一次，卸载时拆干净（铁律 4） ---------- */
  useEffect(() => {
    const cv = cvRef.current;
    if (!cv) return;
    const eng = new AbyssEngine(cv);
    engineRef.current = eng;
    setSample((v) => v + 1);
    return () => {
      eng.dispose();
      if (engineRef.current === eng) engineRef.current = null;
    };
  }, [engineRef]);

  /* ---------- 尺寸：容器变了就重算几何（切视图会改中列宽度） ---------- */
  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const ro = new ResizeObserver(() => {
      engineRef.current?.resize();
      /* 叠层的坐标全变了，立刻重采一次，别等下一个 100ms */
      setSample((v) => v + 1);
    });
    ro.observe(host);
    return () => ro.disconnect();
  }, [engineRef]);

  /* ---------- 主题：装置与界面必须是同一件东西（palette.ts 铁律 2） ----------
     ⚠️ 必须推迟一帧再取色：useTheme 写在 data-theme 的 effect 属于父组件（App），
        而本组件的 effect 比父组件先跑 —— 立刻 refreshPalette() 会读到旧主题，
        装置因此总是比界面慢一拍换材质（血泪：见 错题本.md #10）。
        推迟到下一帧（rAF）时，父组件的 effect 已经把 data-theme 写好了。 */
  useEffect(() => {
    const raf = requestAnimationFrame(() => engineRef.current?.refreshPalette());
    return () => cancelAnimationFrame(raf);
  }, [theme, engineRef]);

  /* ---------- reduced-motion：不是关掉，是离散呈现（engine 铁律 3） ---------- */
  useEffect(() => { engineRef.current?.setReduced(reduced); }, [reduced, engineRef]);

  /* ---------- 数据：只喂事实，画面由装置追上来（铁律 1） ---------- */
  const input = useMemo<AbyssInput>(() => ({
    agents, memories, bonds, probe, vitals, liveness, focusMemoryId, focusAgent,
  }), [agents, memories, bonds, probe, vitals, liveness, focusMemoryId, focusAgent]);
  useEffect(() => { engineRef.current?.setData(input); }, [input, engineRef]);

  /* ---------- 页面不可见就停（铁律 3） ---------- */
  useEffect(() => {
    const on = () => engineRef.current?.setRunning(!document.hidden);
    document.addEventListener('visibilitychange', on);
    on();
    return () => document.removeEventListener('visibilitychange', on);
  }, [engineRef]);

  /* ---------- 覆盖层采样（只在渊谱视图挂了 overlay 时跑） ---------- */
  useEffect(() => {
    if (!overlay) return;
    const id = window.setInterval(() => setSample((v) => v + 1), reduced ? 600 : 100);
    return () => window.clearInterval(id);
  }, [overlay, reduced]);

  /* ---------- 指针 ---------- */
  const localPoint = (e: { clientX: number; clientY: number }) => {
    const cv = cvRef.current;
    if (!cv) return null;
    const b = cv.getBoundingClientRect();
    return { x: e.clientX - b.left, y: e.clientY - b.top };
  };

  const onMove = useCallback((e: React.PointerEvent) => {
    const eng = engineRef.current;
    const p = localPoint(e);
    if (!eng || !p) return;
    const hit = eng.pick(p.x, p.y);
    eng.setHover(hit);
    setTip(hit ? { x: hit.x, y: hit.y, hit } : null);
  }, [engineRef]);

  const onLeave = useCallback(() => {
    engineRef.current?.setHover(null);
    setTip(null);
  }, [engineRef]);

  const onDown = useCallback((e: React.PointerEvent) => {
    const eng = engineRef.current;
    const p = localPoint(e);
    if (!eng || !p) return;
    const hit = eng.pick(p.x, p.y);
    if (!hit) { onPickMemory?.(null); return; }
    if (hit.kind === 'lamp') onPickLamp?.(hit.id);
    else onPickMemory?.(hit.id);
  }, [engineRef, onPickLamp, onPickMemory]);

  /* ---------- 键盘：渊本身可聚焦，方向键在记忆点之间走 ----------
     ⚠️ 这是无障碍底线，不是主路径（库主拍板 Win 端鼠标优先）：
        鼠标能做的这里也能做，但【不】要求库主用键盘操作渊。 */
  const onKey = useCallback((e: React.KeyboardEvent) => {
    const eng = engineRef.current;
    if (!eng) return;
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'Enter' && e.key !== ' ') return;
    const pts = [...eng.nodePoints().entries()].sort((a, b) => a[1].x - b[1].x);
    if (!pts.length) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (tip?.hit?.kind === 'memory') onPickMemory?.(tip.hit.id);
      return;
    }
    e.preventDefault();
    const curId = tip?.hit?.kind === 'memory' ? tip.hit.id : focusMemoryId;
    const idx = pts.findIndex(([id]) => id === curId);
    const next = e.key === 'ArrowRight'
      ? pts[Math.min(pts.length - 1, idx + 1)] ?? pts[0]
      : pts[Math.max(0, idx - 1)] ?? pts[0];
    const node = memories.find((m) => m.id === next[0]);
    if (!node) return;
    const hit: Hit = { kind: 'memory', id: next[0], x: next[1].x, y: next[1].y, node };
    eng.setHover(hit);
    setTip({ x: next[1].x, y: next[1].y, hit });
    onPickMemory?.(next[0]);
  }, [engineRef, tip, focusMemoryId, memories, onPickMemory]);

  /* ---------- 浮层文案：封蜡纪律在这里落地（见文件头） ---------- */
  const tipBody = (() => {
    if (!tip?.hit) return null;
    if (tip.hit.kind === 'lamp') {
      const ag = agents.find((a) => a.id === tip.hit!.id);
      if (!ag) return null;
      return (
        <>
          <div className="k">{ag.epithet}</div>
          <div>{ag.name}｜{ag.can_answer_now ? '能立刻回话' : '只能留帖'}</div>
          <div className="k">
            记忆 <span className="num">{ag.memories}</span> 条 · 封 <span className="num">{ag.private_count}</span> 条 ·
            钟速 <span className="num">{ag.clock_rate.toFixed(2)}</span>
          </div>
        </>
      );
    }
    const m = tip.hit.node;
    const sealed = m.visibility === 'private';
    return (
      <>
        <div className="k">
          {m.tier === 'refined' ? '精炼层' : '全量层'} · {m.owner}
          {m.retired ? ' · 已翻面退役' : ''}
        </div>
        {/* 私密：只写"封"。后端没给正文，这里也不解释为什么（原则 5） */}
        {sealed ? <div className="seal" aria-label="封蜡：只给条数，不给正文" /> : <div>{m.excerpt}</div>}
        <div className="k">
          热度 <span className="num">{pct(m.heat)}</span> · 寿命 <span className="num">{pct(m.life)}</span> ·
          分量 <span className="num">{pct(m.importance)}</span>
        </div>
      </>
    );
  })();

  /* 浮层位置：贴着点，但不越出渊 */
  const tipStyle = (() => {
    if (!tip) return undefined;
    const host = hostRef.current;
    const w = host?.clientWidth ?? 800;
    const left = Math.min(Math.max(8, tip.x + 14), Math.max(8, w - 296));
    return { left, top: Math.max(8, tip.y - 12) };
  })();

  const ov = overlay && engineRef.current
    ? overlay({ points: engineRef.current.nodePoints(), geom: engineRef.current.geom })
    : null;

  return (
    <div
      className="abyss"
      ref={hostRef}
      tabIndex={0}
      role="img"
      aria-label={
        `渊：全量层 ${vitals.raw} 条，精炼层 ${vitals.refined} 条，共 ${vitals.total} 条。` +
        `四盏灯 ${agents.map((a) => `${a.name}${a.can_answer_now ? '在' : '留帖'}`).join('、')}。` +
        `方向键在记忆点之间移动。`
      }
      onPointerMove={onMove}
      onPointerLeave={onLeave}
      onPointerDown={onDown}
      onFocus={() => setFocused(true)}
      onBlur={() => { setFocused(false); onLeave(); }}
      onKeyDown={onKey}
      data-sample={sample}
    >
      <canvas ref={cvRef} />
      {ov}
      {notes && <div className="abyss-note">{notes}</div>}
      {tip && tipBody && <div className="abyss-tip" style={tipStyle}>{tipBody}</div>}
      {/* 焦点摘要：键盘落到渊上时，把"这池水现在什么样"写出来 */}
      {focused && !veil && (
        <div className="abyss-focus">
          <span>渊</span>
          <span>沉 <b className="num">{vitals.raw}</b></span>
          <span>浮 <b className="num">{vitals.refined}</b></span>
          <span>活性 <b className="num">{pct(liveness)}</b></span>
          <span>方向键走点 · Enter 选中</span>
        </div>
      )}
      {veil}
    </div>
  );
}
