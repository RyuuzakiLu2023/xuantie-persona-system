/* ================================================================
   照渊台 · 帖行（总导游图）★ 五等帖的呈现只有这一处 ★

  ▍这是什么
  ① 干什么：一条帖在列表里的样子。帖案、差帖看板、往来记录三处共用
  ② 给谁看：改帖的呈现的人
  ③ 依赖：api/fates.ts（命运表 + 状态脸）、lib/format.ts

  ▍★ 为什么共用一个组件 ★
  spec §T2 要求"视图之间有因果"：投出那枚帖在照渊里、在差帖案里必须是同一枚，
  颜色与状态延续。三处各写一份必然漂成三种样子（fates.ts 铁律 1 同理）。

  ▍★ 命运位显形 ★
  每条帖右上角四个小方格 = persist / notify / needs_ack / audience。
  亮=会发生。库主扫一眼就知道"这条会不会入库、会不会把人叫起来"，
  【不】依赖记住五个标签名。

  ▍修改铁律
  1. 状态文案一律取 STATE_FACE[state].says，【不】另写（见 fates.ts 铁律 1）。
  2. 私密条目：content 恒为 null，画封蜡。【不】写"无权查看"。
  3. 【不】给帖加"删除"动作——契约里没有这个端点（见 endpoints.ts 铁律 1）。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T2）*
================================================================ */

import type { Agent, Message } from '../api/endpoints';
import { STATE_FACE, fateOf } from '../api/fates';
import { nameOf } from '../lib/agents';
import { ago } from '../lib/format';
import { SealRow } from './Parts';

/**
 * 四命运位的小方格。★ 看形状就知道命运（见文件头）★
 * ⚠️ 入参故意收得比 Message 宽松：递帖台要在"还没成帖"时先画一遍命运草图，
 *    那时只有 kind 与四位，没有 id/room_id。所以这里只要 fateOf 需要的那几个字段。
 */
export function FateBits({ m }: { m: Pick<Message, 'kind'> & Partial<Message> }) {
  const f = fateOf(m);
  const bits: [string, boolean, string][] = [
    ['渊', f.persist, f.persist ? '会入渊（永久落库）' : '不入渊（看过即散）'],
    ['扰', f.notify, f.notify ? '会把人叫起来' : '不打扰任何人'],
    ['执', f.needs_ack, f.needs_ack ? '要回执，没回执前一直拴着' : '不要回执'],
    [f.audience === 'room' ? '合' : '对', true, f.audience === 'room' ? '合席可见（整间房）' : '对席可见（两人）'],
  ];
  return (
    <span className="fate-bits" role="group" aria-label="四命运位">
      {bits.map(([ch, on, why]) => (
        <span key={ch} className={`bit${on ? ' on' : ''}`} title={why}>{ch}</span>
      ))}
    </span>
  );
}

interface Props {
  m: Message;
  agents: Agent[];
  now: number;
  /** 选中它（看它的命途） */
  onPick?: (m: Message) => void;
  selected?: boolean;
}

/**
 * MsgRow：契约
 * 入参：m（帖）/ agents（取名字用）/ now（相对时间）/ onPick / selected
 * 返回：列表里的一行
 * 副作用：无
 * 谁调用：照渊的帖流、差帖看板、递帖的往来记录
 * 改动影响：三处共用，改样式会同时改三处——这是故意的（见文件头）。
 */
export function MsgRow({ m, agents, now, onPick, selected }: Props) {
  const f = fateOf(m);
  const face = STATE_FACE[m.state];
  const sealed = m.visibility === 'private';
  const who = m.from === 'ryuuzaki'
    ? `给 ${m.to.map((t) => nameOf(agents, t)).join('、') || '（无）'}`
    : `${nameOf(agents, m.from)} 说`;

  const body = (
    <>
      <span className="row-top">
        <span className="row-name">{f.label} · {who}</span>
        <FateBits m={m} />
      </span>
      {/* 私密：封蜡占位，没有正文可显（后端就没给） */}
      {sealed ? <span className="row-sub"><SealRow slit={false} /></span>
        : <span className="row-sub">{m.content ?? '（空）'}</span>}
      {m.stream && m.state === 'streaming' && (
        <span className="row-sub" style={{ color: 'var(--gold)' }}>{m.stream}</span>
      )}
      <span className="row-tags">
        <span className={`tag${m.state === 'timeout' ? ' amber' : m.state === 'failed' ? ' cinnabar' : m.state === 'acked' ? '' : ' gold'}`}>
          {face.label}
        </span>
        {m.visibility === 'shared_with' && <span className="tag">定向 {m.shared_with?.length ?? 0}</span>}
        {sealed && <span className="tag">封</span>}
        {m.chunk_id && <span className="tag">已入渊</span>}
        <span className="tag">{ago(m.created_at, now)}</span>
      </span>
    </>
  );

  if (!onPick) return <div className="row" aria-label={face.says}>{body}</div>;
  return (
    <button className="row" aria-selected={selected} onClick={() => onPick(m)} title={face.says}>
      {body}
    </button>
  );
}
