/* ================================================================
   照渊台 · 会话输入条 ChatBar（总导游图）★ 主页投帖的唯一入口 ★

  ▍这是什么
  ① 干什么：主页聊天窗口底部的一条：收件人（快速点灯）+ 重量（五等帖）+ 正文 + 投出
  ② 给谁看：改主页投帖交互的人
  ③ 依赖：api/fates（命运表）、lib/agents（灯的说法）、SendDesk 的 Draft

  ▍★ 为什么把完整递帖台简化成一条 ★
  库主拍板（2026-09-02）：主页以会话窗口为主。"落一句话"应该像聊天一样轻，
  重量与可见性作为一排小按钮展开，而不是一整面表单。
  ⚠️ 递帖的高级路径（定向勾人、预演命途）保留在 SendDesk —— 点「重量」那一排的
     「展开」才进完整台，别让主页被一整面表单压住。

  ▍修改铁律
  1. 投出走 App 的 doCast（它还要开命途、驱动装置）。【不】在此处调 api.send。
  2. 收件人必须能即时选（主页是默认入口），【不】默认绑死第一盏灯。
  3. 数字/命运位一律走现成组件（FateBits），【不】重新造轮子。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 重构主页为会话窗口）*
================================================================ */

import type { Agent, Room } from '../api/endpoints';
import type { Visibility } from '../api/contract';
import { FATE, FATE_ORDER, VISIBILITY } from '../api/fates';
import { inLampOrder } from '../lib/agents';
import type { Draft } from './SendDesk';
import { FateBits } from './MsgRow';

interface Props {
  agents: Agent[];
  room: Room | null;
  draft: Draft;
  onPatch: (p: Partial<Draft>) => void;
  onCast: () => void;
  onMore: () => void;
  textRef: React.RefObject<HTMLTextAreaElement | null>;
  busy: boolean;
}

export function ChatBar({ agents, room, draft, onPatch, onCast, onMore, textRef, busy }: Props) {
  const lamps = inLampOrder(agents);
  const set = (p: Partial<Draft>) => onPatch(p);
  const can = !!draft.to && !!room && draft.text.trim().length > 0 && !busy;

  return (
    <div className="chatbar">
      {/* 收件人：一排小灯卡，点亮即选 */}
      <div className="cb-row cb-lamps" role="group" aria-label="递给谁">
        {lamps.map((a) => (
          <button key={a.id} className="cb-lamp" aria-pressed={draft.to === a.id}
            onClick={() => set({ to: draft.to === a.id ? null : a.id })}
            title={`${a.name}：${a.can_answer_now ? '能立刻回话' : '只能留帖'}${a.wake_p50_ms ? ' · 唤醒 ' + Math.round(a.wake_p50_ms / 1000) + 's' : ''}`}>
            {a.name}
          </button>
        ))}
        {!draft.to && <span className="cb-hint">点一盏灯</span>}
      </div>

      {/* 重量：五等帖一排小按钮，命运位显形 */}
      <div className="cb-row cb-fates" role="group" aria-label="多重">
        {FATE_ORDER.map((k) => {
          const f = FATE[k];
          return (
            <button key={k} className="cb-fate" aria-pressed={draft.kind === k}
              onClick={() => set({ kind: k })} title={f.destiny}>
              <span>{f.label}</span>
              <FateBits m={f as never} />
            </button>
          );
        })}
        <button className="cb-more" onClick={onMore} title="进完整递帖台（定向勾人 / 预演命途）">更多…</button>
      </div>

      {/* 正文 + 投出 */}
      <div className="cb-row cb-input">
        <textarea ref={textRef} className="body-input" value={draft.text}
          placeholder={room ? `投进「${room.title}」` : '先选一间房'}
          onChange={(e) => set({ text: e.target.value })}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && can) onCast(); }} />
        <div className="cb-acts">
          <div className="seg" role="group" aria-label="谁看得见">
            {(['private', 'shared', 'shared_with'] as Visibility[]).map((v) => (
              <button key={v} aria-pressed={draft.visibility === v}
                onClick={() => set({ visibility: v })} title={VISIBILITY[v].who}>
                {VISIBILITY[v].label}
              </button>
            ))}
          </div>
          <button className="btn-cast" disabled={!can} onClick={onCast}
            title={can ? '投出（Ctrl+Enter）' : '先点一盏灯，再写一句'}>
            {draft.to && !FATE[draft.kind].notify ? '投下' : draft.to && !(agents.find(a => a.id === draft.to)?.can_answer_now) ? '留帖' : '投下'}
          </button>
        </div>
      </div>
    </div>
  );
}
