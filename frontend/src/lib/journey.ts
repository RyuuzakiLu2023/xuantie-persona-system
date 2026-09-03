/* ================================================================
   照渊台 · 命途导演（总导游图）★ 灵魂交互的中枢，改动前务必读懂 ★

  ▍这是什么
  ① 干什么：把假后端裁决出的「幕次表」（mock 的 Journey）翻译成两件事：
     ——装置动作（cast / kickLamp / emitSparks / echo / amber / dissolve）
     ——时间轴的显示状态（走到第几幕、当前幕是哪一幕）
  ② 给谁看：改命途表现、加新幕、调节奏的人
  ③ 依赖：mock/engine.ts 的 Journey/Act、api/fates.ts 的 FATE、device/engine.ts

  ▍★ 三段时钟的分工（spec §T3 的三段实现约定）★
  1. 假后端的"现在"   mock 的 tick(now)，纯函数推进，可 skip 快进 —— 事实在这里
  2. 命途的幕次表     verdict() 裁决出的 Act[]（at_ms/says/who/negated）—— 剧本在这里
  3. 前端 60fps 时钟  useFrameClock —— 只负责"这一帧该画到哪儿"
  ⚠️ 三者【不】许混用：事实归轮询与 mock，动效归本地时钟。混了就会出现
     "网络抖一下动画就跳帧"，或"快进后时间轴不动"（见 client.mockNow 注释）。

  ▍★ 一幕只放一次 ★
  装置动作是"事件"不是"状态"：涟漪、爆芯、回声只该在越过那一幕的瞬间放一次。
  所以用 fired（"放映记录"）记住已经放过的幕；流式火星是持续现象，例外处理
  （见 STREAM_EVERY_MS）。漏了这条，每帧都会重放一次爆芯 —— 灯会疯狂闪。

  ▍外号表
  "放映记录" → fired，记住哪几幕的装置动作已经放过（同 engine 的"小本本"思路）
  "导演"     → useJourneyDirector，唯一把幕翻成装置动作的地方

  ▍修改铁律
  1. 加一幕：先在 mock/engine.ts 的 ACTS 与 stateOfAct() 里加（那是事实层），
     再在本文件 fire() 里给它装置动作，最后在 FateTimeline 的 glyph 里给它符号。
     缺一头就会出现"时间轴有这一幕，渊里什么也没发生"。
  2. 【不】在本文件造事实（不改 message、不改 graph）。它只读幕、只驱动画面。
  3. 装置动作【不】许在 render 里调，只能在 effect 里调（React 的 render 必须纯）。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T3）*
================================================================ */

import { useEffect, useMemo, useRef, useState } from 'react';
import type { Message } from '../api/endpoints';
import { fateOf } from '../api/fates';
import type { AbyssEngine } from '../device/engine';
import type { Act, Journey } from '../mock/engine';
import { journeyOf, mockNow } from './client';
import { useFrameClock, usePrefersReducedMotion } from './hooks';

/** 流式火星的补给节奏：这是持续现象，不受"一幕只放一次"约束（见文件头）。 */
const STREAM_EVERY_MS = 260;

export interface JourneyView {
  journey: Journey | null;
  /** 自投出起的毫秒数（按假后端的现在算） */
  elapsed: number;
  /** 已经走过的幕数 */
  doneCount: number;
  /** 当前那一幕（最后一幕已过则为最后一幕） */
  current: Act | null;
  /** 全部走完了没有 */
  finished: boolean;
}

/**
 * useJourneyDirector()：契约
 * 入参：engineRef（装置）/ msg（正在看的那条帖，可空）/ tracking（是否驱动装置）
 * 返回：JourneyView（给时间轴显示用）
 * 副作用：★ 调装置的 cast/kickLamp/emitSparks/echo/amber/dissolve ★
 * 谁调用：App（只调一次，避免两处同时驱动同一个装置）
 * 改动影响：⚠️ 这是"投一枚帖会看见什么"的全部来源。加幕见铁律 1。
 */
export function useJourneyDirector(
  engineRef: { current: AbyssEngine | null },
  msg: Message | null,
  tracking: boolean,
): JourneyView {
  const reduced = usePrefersReducedMotion();
  /* 本地时钟只在真的有帖在走时才跑，别让空闲页面一直 rAF */
  const t = useFrameClock(tracking && !!msg);
  const [, bump] = useState(0);

  /** 放映记录：key = msg_id + '#' + 幕序号 */
  const fired = useRef(new Set<string>());
  const lastSpark = useRef(0);
  /* 换了一条帖就清空放映记录：新帖要从头演 */
  const curId = useRef<string | null>(null);
  if (msg && curId.current !== msg.id) {
    curId.current = msg.id;
    fired.current = new Set();
    lastSpark.current = 0;
  }

  const journey = useMemo<Journey | null>(
    () => (msg ? journeyOf(msg.id) ?? null : null),
    /* t 进来是为了让"刚发出的帖"在下一帧就能取到命途（send 与 render 有一帧差） */
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
    [msg?.id, t === 0],
  );

  const elapsed = journey ? Math.max(0, mockNow() - journey.t0) : 0;
  const done = journey ? journey.acts.filter((a) => a.at_ms <= elapsed) : [];
  const current = done.length ? done[done.length - 1] : null;

  /* ---------- 把幕翻成装置动作（唯一出处，见铁律 1） ---------- */
  useEffect(() => {
    const eng = engineRef.current;
    if (!eng || !journey || !msg || !tracking) return;
    const f = fateOf(msg);

    journey.acts.forEach((act, i) => {
      if (act.at_ms > elapsed) return;
      const key = `${journey.msg_id}#${i}`;
      if (fired.current.has(key)) return;
      fired.current.add(key);

      switch (act.kind) {
        case 'cast':
          /* 投物入水：形态与深度全由四命运位派生（见 fates.ts） */
          eng.cast({
            id: msg.id, to: journey.to, form: f.stone,
            depth: f.depth, weight: f.weight, tether: f.needs_ack,
          });
          break;
        case 'wake':
          /* 爆芯：只有会打扰的帖才把人叫起来；不打扰的帖轻轻碰一下 */
          eng.kickLamp(journey.to, f.notify ? 1.6 : 0.5);
          break;
        case 'stream':
          eng.emitSparks(journey.to, reduced ? 0 : 8);
          break;
        case 'reply':
          eng.echo();
          break;
        case 'timeout':
          /* 叫不醒：水面那枚帖转琥珀。内容不丢，只是变成留帖 */
          eng.amber(msg.id);
          break;
        case 'evict':
        case 'superseded':
          if (msg.chunk_id) eng.dissolve(msg.chunk_id);
          break;
        default:
          /* ripple / sink / ack / distill / hit / seen 没有独立装置动作：
             它们的画面来自数据本身（记忆点的层/热度/翻面由 setData 带进来）。
             ⚠️ 这不是漏做——那些幕改的是"事实"，事实由 mock 改完再经轮询流回来。 */
          break;
      }
    });
  }, [engineRef, journey, msg, elapsed, tracking, reduced]);

  /* ---------- 流式火星的持续补给（例外，见文件头） ---------- */
  useEffect(() => {
    const eng = engineRef.current;
    if (!eng || !journey || !tracking || reduced) return;
    const streamAt = journey.acts.find((a) => a.kind === 'stream')?.at_ms;
    const replyAt = journey.acts.find((a) => a.kind === 'reply')?.at_ms;
    if (streamAt === undefined) return;
    const inStream = elapsed >= streamAt && (replyAt === undefined || elapsed < replyAt);
    if (!inStream) return;
    const now = performance.now();
    if (now - lastSpark.current < STREAM_EVERY_MS) return;
    lastSpark.current = now;
    eng.emitSparks(journey.to, 3);
  }, [engineRef, journey, elapsed, tracking, reduced]);

  /* reduced-motion 下没有 rAF，靠这个秒级心跳让时间轴照旧往前走
     （信息照送，只是不连续 —— 见 hooks.useFrameClock 的注释） */
  useEffect(() => {
    if (!reduced || !tracking || !msg) return;
    const id = window.setInterval(() => bump((v) => v + 1), 1000);
    return () => window.clearInterval(id);
  }, [reduced, tracking, msg]);

  return {
    journey,
    elapsed,
    doneCount: done.length,
    current,
    finished: !!journey && elapsed >= journey.span_ms,
  };
}
