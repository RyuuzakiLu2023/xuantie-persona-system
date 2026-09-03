/* ================================================================
   照渊台 · 轮询与状态钩子（总导游图）

  ▍这是什么
  ① 干什么：只用 HTTP 轮询（2–5 秒）把八个端点的数据喂给界面，并管住三种态
  ② 给谁看：加视图、改刷新节奏的人
  ③ 依赖：lib/client.ts

  ▍★ 为什么"实时感"不靠连接技术 ★
  契约写死只用轮询。所以：
    · 数据的新鲜度由轮询保证（够了，人的操作节奏是秒级）
    · "正在干活"的观感由假后端的流式累积 + 界面的逐字呈现给出
    · 一条帖走链的紧张感由 useJourneyClock 的本地时钟给出（60fps 级，不打网络）
  【不】自建 WebSocket——加了就等于在契约外开后门（见 vite.config.ts 铁律 3）。

  ▍结构
  usePoll         通用轮询：可见性感知（页面藏起来就停）、退避、保留上次成功值
  useCockpit      /api/cockpit/state
  useRooms        /api/rooms
  useRoomContext  /api/room/{id}/context
  useGraph        /api/graph/memory（图大，节流成 state 的三倍间隔）
  useJourneyClock 本地时钟：给动效编排用，不打网络
  useNow          秒级时钟：给相对时间文案用

  ▍修改铁律
  1. 轮询失败【不】清空旧数据。旧数据 + stale 标记，是降级态的地基；
     清空等于把"渊面结冰还能看到上一眼水"这个设计砸掉。
  2. 页面不可见时必须停轮询（document.hidden）。这台工具每天开几十次，
     后台空转是真实的电费与风扇噪音。
  3. 加新端点用 usePoll，【不】另写一套 setInterval。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ApiFault, CockpitState, MemoryGraph, RoomContext, RoomsRes } from '../api/endpoints';
import { api, flags, isFault } from './client';

/** 一条轮询腿的对外形状。★ ok + fault 同时存在 = 降级态 ★ */
export interface Leg<T> {
  /** 上次成功的数据。失败时保留，不清空（铁律 1） */
  data: T | null;
  /** 当下的故障。data 非空 + fault 非空 = 降级：看的是旧水 */
  fault: ApiFault | null;
  /** 有没有取过一次（用于区分"空态"和"还没加载"） */
  loaded: boolean;
  /** 上次成功时刻 */
  at: number | null;
  /** 手动重取 */
  refresh: () => void;
}

function usePoll<T>(fn: () => Promise<T | ApiFault>, ms: number, deps: unknown[] = []): Leg<T> {
  const [data, setData] = useState<T | null>(null);
  const [fault, setFault] = useState<ApiFault | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [at, setAt] = useState<number | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  /* 连错退避：错一次翻倍，最多 4 倍，成功即复位——别把后端捶死 */
  const backoff = useRef(1);
  const timer = useRef<number | null>(null);

  const run = useCallback(async () => {
    const r = await fnRef.current();
    setLoaded(true);
    if (isFault(r)) {
      setFault(r);
      backoff.current = Math.min(4, backoff.current * 2);
    } else {
      setData(r);
      setFault(null);
      setAt(Date.now());
      backoff.current = 1;
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const loop = async () => {
      if (!alive) return;
      if (!document.hidden) await run();
      if (!alive) return;
      timer.current = window.setTimeout(loop, ms * backoff.current);
    };
    void loop();
    const onVis = () => { if (!document.hidden) void run(); };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      alive = false;
      document.removeEventListener('visibilitychange', onVis);
      if (timer.current) window.clearTimeout(timer.current);
    };
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [ms, run, ...deps]);

  return { data, fault, loaded, at, refresh: () => void run() };
}

export function useCockpit(): Leg<CockpitState> {
  const { pollMs } = useMemo(flags, []);
  return usePoll<CockpitState>(() => api.state(), pollMs);
}

export function useRooms(): Leg<RoomsRes> {
  const { pollMs } = useMemo(flags, []);
  return usePoll<RoomsRes>(() => api.rooms(), pollMs);
}

export function useRoomContext(roomId: string | null, topicId: string | null): Leg<RoomContext> {
  const { pollMs } = useMemo(flags, []);
  return usePoll<RoomContext>(
    () => (roomId ? api.roomContext(roomId, topicId) : Promise.resolve({ ok: false, code: 'bad_shape', message: '没选房间' } as ApiFault)),
    pollMs,
    [roomId, topicId],
  );
}

/** 图数据大（十万量级字节），按 state 的三倍间隔取——它不需要那么新鲜。 */
export function useGraph(): Leg<MemoryGraph> {
  const { pollMs } = useMemo(flags, []);
  return usePoll<MemoryGraph>(() => api.graph(), pollMs * 3);
}

/* ================================================================
   本地时钟 — 动效编排用，不打网络
   ⚠️ 这是"实时感"的真正来源（见文件头）。它跟轮询解耦：
   轮询决定"事实"，本地时钟决定"这一帧画到哪儿"。
   ================================================================ */

/** 逐帧时钟：返回自挂载起的毫秒数。reduced-motion 下降为秒级步进。 */
export function useFrameClock(active = true): number {
  const [t, setT] = useState(0);
  const reduce = usePrefersReducedMotion();
  useEffect(() => {
    if (!active) return;
    if (reduce) {
      /* ⚠️ reduced-motion 的替代方案【不】是关掉动画，而是把连续运动换成
         每秒一次的离散推进——信息照旧送到，只是不再有连续位移。 */
      const id = window.setInterval(() => setT((v) => v + 1000), 1000);
      return () => window.clearInterval(id);
    }
    let raf = 0;
    const t0 = performance.now();
    const step = (now: number) => { setT(now - t0); raf = requestAnimationFrame(step); };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [active, reduce]);
  return t;
}

/** 秒级时钟：相对时间文案用，别为了"刚刚"两个字每帧重渲染。 */
export function useNow(stepMs = 1000): number {
  const [n, setN] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setN(Date.now()), stepMs);
    return () => window.clearInterval(id);
  }, [stepMs]);
  return n;
}

export function usePrefersReducedMotion(): boolean {
  const [v, setV] = useState(() =>
    typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)').matches : false);
  useEffect(() => {
    const mq = matchMedia('(prefers-reduced-motion: reduce)');
    const on = () => setV(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  return v;
}

/** 窄屏判定：手机上这台操作台是"能看能回一句"的形态。 */
export function useNarrow(px = 860): boolean {
  const [v, setV] = useState(() => (typeof matchMedia === 'function' ? matchMedia(`(max-width:${px}px)`).matches : false));
  useEffect(() => {
    const mq = matchMedia(`(max-width:${px}px)`);
    const on = () => setV(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [px]);
  return v;
}
