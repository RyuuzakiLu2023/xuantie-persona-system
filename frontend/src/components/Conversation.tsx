/* ================================================================
   照渊台 · 会话窗口 Conversation（总导游图）★ 主页主体 ★

  ▍这是什么
  ① 干什么：把一间房/一个话题的往来记录渲染成"聊天窗口"——气泡式消息流
  ② 给谁看：改主页布局、改消息气泡呈现的人
  ③ 依赖：api/endpoints（RoomContext/Message）、api/fates（命运表）、lib/agents

  ▍★ 为什么是气泡而不是列表 ★
  库主拍板（2026-09-02）：主页以会话窗口为主。消息流用"谁说的、从左还是从右"
  直接可读，比一张张平铺的卡片更接近"跟这些人说话"——而不是"看一堆记录"。

  ▍★ 气泡的三条纪律 ★
  1. 私密条目：content=null，气泡内只画封蜡，【不】显示正文、也【不】写"无权查看"。
  2. 流式回传：state=streaming 且带 stream 时，气泡内逐字呈现（后端累积下发，前端追加）。
  3. 超时/叫不醒：状态转琥珀，但【不】把正文藏起来——内容是留帖，不是被删。

  ▍修改铁律
  1. 【不】在会话窗口里调 api.send()。投出走 App 的 doCast（它还要开命途、驱动装置）。
  2. 气泡分组【不】做复杂合并——先每条一泡（跑通链路优先），分组留到界面打磨轮。
  3. 数字一律 lib/format.group()；状态文案取 STATE_FACE[s].says。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 重构主页为会话窗口）*
================================================================ */

import type { Agent, Message, RoomContext, ApiFault } from '../api/endpoints';
import { STATE_FACE, fateOf } from '../api/fates';
import { nameOf } from '../lib/agents';
import { ago } from '../lib/format';
import { SealRow } from './Parts';
import { FateBits } from './MsgRow';

interface Props {
  ctx: RoomContext | null;
  agents: Agent[];
  now: number;
  trackedId: string | null;
  onPick: (m: Message) => void;
  fault: ApiFault | null;
}

/** 单条气泡。靠 m.from 判左右：库主在右，灯在左。 */
function Bubble({ m, agents, now, onPick, selected }: {
  m: Message; agents: Agent[]; now: number;
  onPick: (m: Message) => void; selected: boolean;
}) {
  const me = m.from === 'ryuuzaki';
  const f = fateOf(m);
  const face = STATE_FACE[m.state];
  const sealed = m.visibility === 'private';
  const name = me ? '库主' : nameOf(agents, m.from as never);
  const system = m.kind === 'system';

  return (
    <div className={`bubble-row${me ? ' me' : ''}${system ? ' system' : ''}${selected ? ' sel' : ''}`}
      onClick={() => onPick(m)} role="button" tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter') onPick(m); }}
      aria-label={`${name}（${f.label}）：「${sealed ? '封蜡' : (m.content ?? '空')}」${face.says}`}>
      {system ? (
        /* 风讯：居中细字，像渊自己落的一句话 */
        <div className="bubble-system">
          <span className="bs-say">{sealed ? <SealRow /> : m.content}</span>
          <span className="bs-meta">{face.label} · {ago(m.created_at, now)}</span>
        </div>
      ) : (
        <>
          <span className="bubble-avatar" aria-hidden>{me ? '主' : (name[0] ?? '灯')}</span>
          <div className="bubble-wrap">
            <span className="bubble-head">
              <span className="bubble-name">{name}</span>
              <FateBits m={m} />
            </span>
            <span className="bubble-body">
              {sealed
                ? <SealRow label="封蜡：只给条数，不给正文" />
                : m.content ?? '（空）'}
              {m.stream && m.state === 'streaming' && (
                <span className="bubble-stream">{m.stream}<span className="caret">▌</span></span>
              )}
            </span>
            <span className="bubble-foot">
              <span className={`tag${m.state === 'timeout' ? ' amber' : m.state === 'failed' ? ' cinnabar' : ' gold'}`}>
                {face.label}
              </span>
              <span>{ago(m.created_at, now)}</span>
              {m.visibility === 'shared_with' && <span className="tag">定向 {m.shared_with?.length ?? 0}</span>}
              {m.chunk_id && <span className="tag">已入渊</span>}
            </span>
          </div>
        </>
      )}
    </div>
  );
}

export function Conversation({ ctx, agents, now, trackedId, onPick, fault }: Props) {
  if (!ctx) {
    return (
      <div className="conv-empty">
        <div className="veil-title">还没挑一间房</div>
        <div className="veil-text">从左边「石舫与横档」选一间，或点一盏灯开聊。</div>
      </div>
    );
  }
  if (fault) {
    return (
      <div className="conv-empty">
        <div className="fault hard" role="status"><b>会话读不出来</b><div>{fault.message}</div></div>
      </div>
    );
  }
  const msgs = ctx.messages;
  return (
    <div className="conv" aria-label={`${ctx.room.title} 的往来`}>
      {msgs.length === 0 && (
        <div className="conv-empty">
          <div className="veil-title">这间房还空着</div>
          <div className="veil-text">落一句话下去，涟漪就会推过去。</div>
        </div>
      )}
      {msgs.map((m) => (
        <Bubble key={m.id} m={m} agents={agents} now={now}
          onPick={onPick} selected={trackedId === m.id} />
      ))}
    </div>
  );
}
