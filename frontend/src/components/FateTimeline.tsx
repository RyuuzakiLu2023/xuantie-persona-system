/* ================================================================
   照渊台 · 命途时间轴（总导游图）★ 本作与"聊天界面"的分界线 ★

  ▍这是什么
  ① 干什么：把一条帖的命途画成一条纵向时间轴——每幕一行（符号 + 时刻 + 说辞），
     当前幕高亮，超时幕转琥珀，"叫不醒/照不到"的幕画出折角
  ② 给谁看：改幕的呈现、加幕符号的人
  ③ 依赖：lib/journey.ts（导演给的 JourneyView）、mock/engine.ts 的 ActKind

  ▍★ 为什么是时间轴而不是气泡 ★
  聊天界面回答"谁说了什么"；这台操作台要回答"这条帖此刻在哪、还会经历什么"。
  所以幕次表【必须】把未来的幕也列出来（灰着），让库主看到"还有回执、还有蒸馏"——
  这就是"长时序编排完整呈现开始—发展—转折—收束"的落点（spec §T3）。

  ▍文件从上到下的结构
  ACT_FACE     每一幕的中文名与符号（★ 加幕必须加这里，见 journey.ts 铁律 1 ★）
  FateTimeline 头（收件人 + 五等帖名）→ 幕列表 → 流式正文 → 脚（快进 / 关）

  ▍★ 转折要看得见 ★
  timeout / negated 两类幕是"事情没按预期走"：
    timeout  折角符号 + 琥珀色（内容不丢，只是变成留帖）
    negated  断线符号 + 琥珀色（可见性挡住了：照不到）
  【不】用红色——红是"淘汰与危险"专用（见 tokens.css 配额）。

  ▍修改铁律
  1. 未来的幕【不】许隐藏。看不见后续，库主就无法判断"还要等什么"。
  2. 幕的说辞直接用 mock 给的 says，【不】在这里另写一套文案——
     两处文案必然漂（血泪：改了机制忘了改 UI 文案，UI 就在说谎）。
  3. 【不】用 Emoji 做幕符号（零 Emoji 是硬底线），符号一律内联 SVG 描线。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T3）*
================================================================ */

import type { Message } from '../api/endpoints';
import { FATE } from '../api/fates';
import type { ActKind } from '../mock/engine';
import { nameOf } from '../mock/engine';
import type { JourneyView } from '../lib/journey';

/** 一幕的脸：中文名 + 符号。★ 加幕必须加这里 ★（见文件头铁律） */
const ACT_FACE: Record<ActKind, { label: string; glyph: string }> = {
  cast: { label: '投出', glyph: 'M4 4l8 8M12 12l-1.5-4M12 12l-4-1.5' },
  ripple: { label: '涟漪', glyph: 'M2 8c2-2 4-2 6 0s4 2 6 0' },
  wake: { label: '爆芯', glyph: 'M8 2v4M8 10v4M2 8h4M10 8h4' },
  stream: { label: '流式', glyph: 'M3 3l4 4M9 5l3 3M5 9l3 3' },
  reply: { label: '回声', glyph: 'M3 8a5 5 0 0 1 10 0M5 11a3 3 0 0 1 6 0' },
  timeout: { label: '叫不醒', glyph: 'M3 3h7l3 3v7M3 3l10 10' },
  sink: { label: '沉底', glyph: 'M8 2v10M5 9l3 3 3-3' },
  ack: { label: '回执', glyph: 'M3 8l3 3 7-7' },
  distill: { label: '上浮', glyph: 'M8 14V4M5 7l3-3 3 3' },
  hit: { label: '被查到', glyph: 'M8 2v12M4 6l4-4 4 4' },
  seen: { label: '被看见', glyph: 'M2 8s2.5-4 6-4 6 4 6 4-2.5 4-6 4-6-4-6-4z' },
  superseded: { label: '被取代', glyph: 'M3 5h10M3 11h10M11 3l2 2-2 2' },
  evict: { label: '带走', glyph: 'M4 3h8M4 13h8M5 3c0 3 3 4 3 5s-3 2-3 5' },
};

const G = ({ d }: { d: string }) => (
  <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
    stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
    <path d={d} />
  </svg>
);

interface Props {
  view: JourneyView;
  msg: Message;
  /** 流式回传的正文（来自轮询到的 message.stream，逐句累积） */
  stream: string | null | undefined;
  onSkip: () => void;
  onClose: () => void;
  /** 假后端下才有快进 */
  canSkip: boolean;
}

/**
 * FateTimeline：契约
 * 入参：view（导演给的进度）/ msg（那条帖）/ stream（流式正文）/ onSkip / onClose / canSkip
 * 返回：右下角那块时间轴
 * 副作用：无（纯显示；装置动作在 lib/journey.ts 里）
 * 谁调用：App
 * 改动影响：加幕见文件头铁律 1（ACT_FACE 必须同步）。
 */
export function FateTimeline({ view, msg, stream, onSkip, onClose, canSkip }: Props) {
  const { journey, elapsed, doneCount } = view;
  if (!journey) return null;
  const f = FATE[msg.kind];

  return (
    <aside className="fate" aria-label="命途时间轴">
      <div className="corner tl" /><div className="corner tr" />
      <div className="fate-head">
        <div>
          <div className="fate-title">命途</div>
          <div className="panel-note">
            {f.label} → {nameOf(journey.to)}
            {msg.visibility === 'private' ? ' · 封' : msg.visibility === 'shared_with' ? ' · 定向' : ''}
          </div>
        </div>
        <button className="btn btn-icon" onClick={onClose} title="收起命途" aria-label="收起命途">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden>
            <path d="M4 4l8 8M12 4l-8 8" />
          </svg>
        </button>
      </div>

      <div className="fate-body">
        {journey.acts.map((act, i) => {
          const passed = i < doneCount;
          const cur = i === doneCount - 1;
          const face = ACT_FACE[act.kind];
          /* 转折：叫不醒 与 照不到 都标 neg（琥珀），见文件头 */
          const neg = act.kind === 'timeout' || act.negated;
          return (
            <div
              key={i}
              className={`act${passed ? ' done' : ''}${cur ? ' cur' : ''}${neg && passed ? ' neg' : ''}`}
            >
              <span className="act-glyph"><G d={face.glyph} /></span>
              <span className="act-at">
                {act.at_ms === 0 ? '0.0s' : `${(act.at_ms / 1000).toFixed(1)}s`}
              </span>
              <span className="act-says">{act.says}</span>
            </div>
          );
        })}

        {/* 流式正文：逐句累积（mock 的 stream 字段），加一枚光标说明"还在写" */}
        {stream && (
          <div className="stream-text">
            {stream}
            {doneCount > 0 && journey.acts[doneCount - 1]?.kind === 'stream' && <span className="caret">▌</span>}
          </div>
        )}
      </div>

      <div className="fate-foot">
        <span className="panel-note">
          <span className="num">{Math.min(doneCount, journey.acts.length)}</span>
          /<span className="num">{journey.acts.length}</span> 幕 ·
          <span className="num"> {(elapsed / 1000).toFixed(1)}</span>s
        </span>
        <span style={{ flex: 1 }} />
        {canSkip && (
          <button className="btn" onClick={onSkip} title="把命途快进 8 秒（演练用）">
            快进 8s<span className="btn-key">s</span>
          </button>
        )}
      </div>
    </aside>
  );
}
