/* ================================================================
   照渊台 · 四条侧栏（总导游图）

  ▍这是什么
  ① 干什么：四个视图各自的侧栏内容——推格（widget）/ 帖流 / 差帖案 / 房间册 / 实体名单
  ② 给谁看：改侧栏内容与排序的人
  ③ 依赖：api/endpoints（形状）、components/MsgRow、components/Parts、lib/format

  ▍文件从上到下的结构
  WidgetGrid   四盏灯主动推上来的格子（含「梦境」私密格 → 封蜡）
  RecentRail   照渊视图右栏：最近的帖（点一条看它的命途）
  TaskBoard    差帖视图右栏：未回执的差帖 + 今日了结
  RoomRail     递帖视图左栏：房 → 话题；群聊显示轮次与还没回话的人
  EntityRail   渊谱视图左栏：实体名单（★ idf 低的压暗 = 枢纽噪声压制 ★）

  ▍★ 三处纪律 ★
  1. 私密一律走 SealRow（只给条数）——【不】显示正文，【不】写"无权查看"
  2. 帖的呈现一律走 MsgRow（三处共用，保证"同一枚帖"在各视图里长一个样）
  3. 数字一律 .num + group()

  ▍修改铁律
  1. 【不】在侧栏里直接 fetch/调 api——动作一律由 App 传回调（网络出口只有 client.ts）。
  2. 排序规则写在注释里说清"为什么这么排"，别让下一个人以为可以随便改。
  3. 零 Emoji。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T2）*
================================================================ */

import type { AgentId } from '../api/contract';
import type { Agent, Message, Room, Widget, EntityNode } from '../api/endpoints';
import { MsgRow } from './MsgRow';
import { SealRow } from './Parts';
import { nameOf } from '../lib/agents';
import { modeName } from '../mock/engine';
import { ago, group } from '../lib/format';
import { IconRoom, IconSheet } from '../ui/Glyphs';

/* ================================================================
   推格 — 某盏灯主动推上来的一格
   ⚠️ kind=dream 的「梦境」多半是私密（两盏灯每周共同蒸馏），
      body=null + redacted=true → 封蜡，只给存在感不给内容。
   ================================================================ */
export function WidgetGrid({ widgets, agents, now, onAction }: {
  widgets: Widget[];
  agents: Agent[];
  now: number;
  onAction: (widgetId: string, actionId?: string) => void;
}) {
  if (!widgets.length) return null;
  return (
    <div className="rows" role="group" aria-label="灯推上来的格子">
      {widgets.map((w) => (
        <div key={w.id} className="task-card">
          <span className="row-top">
            <span className="row-name">{w.title}</span>
            <span className="tag">{nameOf(agents, w.from)}</span>
          </span>
          {w.body === null
            ? <SealRow label="封蜡：这一格是私密的梦境，只给存在感" />
            : <span className="task-text">{w.body}</span>}
          <span className="task-meta">
            <span className="tag">{w.kind === 'dream' ? '梦境' : w.kind === 'ask' ? '待拍板' : w.kind === 'metric' ? '数' : '记'}</span>
            <span>{ago(w.created_at, now)}</span>
          </span>
          {/* 卡片按钮走同一个端点 POST /api/task/ack（契约合并，见 endpoints.ts） */}
          {w.actions?.length ? (
            <span className="task-acts">
              {w.actions.map((a) => (
                <button key={a.id} className="btn" onClick={() => onAction(w.id, a.id)} title={`就「${w.title}」拍板：${a.label}`}>
                  {a.label}
                </button>
              ))}
            </span>
          ) : null}
        </div>
      ))}
    </div>
  );
}

/* ================================================================
   帖流 — 照渊视图右栏
   排序：直接用后端给的 recent 顺序（后端已按时间倒序）。
   ⚠️ 【不】在前端二次排序：那样"我刚投的在最上面"这件事会被前端的排序偷偷改掉。
   ================================================================ */
export function RecentRail({ recent, agents, now, onPick, trackedId }: {
  recent: Message[];
  agents: Agent[];
  now: number;
  onPick: (m: Message) => void;
  trackedId: string | null;
}) {
  if (!recent.length) {
    return <div className="empty-note">水面还没有一枚帖。<br />按「递帖」投一枚下去。</div>;
  }
  return (
    <div className="rows">
      {recent.map((m) => (
        <MsgRow key={m.id} m={m} agents={agents} now={now}
          onPick={onPick} selected={trackedId === m.id} />
      ))}
    </div>
  );
}

/* ================================================================
   差帖案 — 未回执的差帖 + 今日了结
   ⚠️ 只有 needs_ack 的帖才该出现在这里（命运位说了算，不是 kind 名字说了算）。
      判定走 m.needs_ack（后端可覆盖），【不】写 kind === 'task'（见 fates.ts 铁律 2）。
   ================================================================ */
export function TaskBoard({ recent, agents, now, onAck, onPick, trackedId, doneToday }: {
  recent: Message[];
  agents: Agent[];
  now: number;
  onAck: (m: Message) => void;
  onPick: (m: Message) => void;
  trackedId: string | null;
  doneToday: number;
}) {
  const open = recent.filter((m) => m.needs_ack && m.state !== 'acked');
  const settled = recent.filter((m) => m.needs_ack && m.state === 'acked');

  return (
    <div className="rows">
      <div className="group-title">还没了结（{open.length}）</div>
      {open.length === 0 && <div className="empty-note">锚链都解开了，今天没有欠着的活。</div>}
      {open.map((m) => (
        <div key={m.id} className="task-card" data-state={m.state}>
          <MsgRow m={m} agents={agents} now={now} onPick={onPick} selected={trackedId === m.id} />
          <span className="task-acts">
            <button className="btn" onClick={() => onAck(m)}
              title="回执：锚链解开，这活了结（这一下渊里看得见）">
              回执<span className="btn-key">r</span>
            </button>
          </span>
        </div>
      ))}

      <div className="group-title">今日了结（{group(doneToday)}）</div>
      {settled.length === 0 && <div className="empty-note">还没有了结的。</div>}
      {settled.map((m) => (
        <div key={m.id} className="task-card" data-state="acked">
          <MsgRow m={m} agents={agents} now={now} />
        </div>
      ))}
    </div>
  );
}

/* ================================================================
   房间册 — 递帖视图左栏
   房是渊边的石舫，话题是舫上的横档（见 docs/设计说明.md 七件事对照表）。
   群聊多两件事：发言模式 + 本轮还没回话的人（这是群聊最有信息量的一格）。
   ================================================================ */
export function RoomRail({ rooms, agents, now, roomId, topicId, onPickRoom, onPickTopic, onSchedule }: {
  rooms: Room[];
  agents: Agent[];
  now: number;
  roomId: string | null;
  topicId: string | null;
  onPickRoom: (id: string) => void;
  onPickTopic: (id: string | null) => void;
  onSchedule: (id: string) => void;
}) {
  if (!rooms.length) return <div className="empty-note">还没有一间房。</div>;
  return (
    <div className="rows">
      {rooms.map((r) => {
        const on = roomId === r.id;
        return (
          <div key={r.id}>
            <button className="row" aria-selected={on} onClick={() => { onPickRoom(r.id); onPickTopic(null); }}
              title={r.type === 'group' ? `合席房 · ${modeName(r.speak_mode)}` : '对席房（两人）'}>
              <span className="row-top">
                <span className="row-name">
                  {r.type === 'group' ? <IconRoom size={13} /> : <IconSheet size={13} />} {r.title}
                </span>
                {r.unread > 0 && <span className="tag gold">未读 {r.unread}</span>}
              </span>
              <span className="row-sub">{ago(r.last_at, now)} · {r.topics.length} 个话题</span>
              {r.type === 'group' && (
                <span className="row-tags">
                  <span className="tag">{modeName(r.speak_mode)}</span>
                  <span className="tag">第 {r.round} 轮 · {r.round_state === 'running' ? '进行中' : r.round_state === 'waiting' ? '等回话' : r.round_state === 'closed' ? '已收' : '未开'}</span>
                  {/* 还没回话的人 = 灯影缺位 */}
                  {r.pending_speakers.length > 0 && (
                    <span className="tag amber">等 {r.pending_speakers.map((p: AgentId) => nameOf(agents, p)).join('、')}</span>
                  )}
                </span>
              )}
            </button>

            {/* 展开的房才列话题与「开一轮」，否则左栏会长成一堵墙 */}
            {on && (
              <div className="rows" style={{ margin: '6px 0 2px 10px' }}>
                {r.topics.map((t) => (
                  <button key={t.id} className="row" aria-selected={topicId === t.id}
                    onClick={() => onPickTopic(topicId === t.id ? null : t.id)}
                    title={`${t.title}：${t.msg_count} 条${t.private_count ? `，其中 ${t.private_count} 条封着` : ''}`}>
                    <span className="row-top">
                      <span className="row-name" style={{ fontSize: 12 }}>{t.title}</span>
                      <span className="tag">{t.msg_count}</span>
                    </span>
                    {t.private_count > 0 && (
                      <span className="row-sub"><SealRow count={t.private_count} /></span>
                    )}
                  </button>
                ))}
                {r.type === 'group' && (
                  <button className="btn" onClick={() => onSchedule(r.id)}
                    title={`按${modeName(r.speak_mode)}开一轮：排到的灯会真的开口`}>
                    开一轮（{modeName(r.speak_mode)}）
                  </button>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/* ================================================================
   实体名单 — 渊谱视图左栏
   ★ idf 低的压暗，就是"逆文档频率压制枢纽噪声"这条算法的界面样子 ★
   排序：idf 高（稀有、有信息量）在前，枢纽噪声沉底。
   ⚠️ aliases 是"把散落称呼归一"的证据，必须显示出来——否则那条算法白做了。
   ================================================================ */
export function EntityRail({ entities, focus, onPick }: {
  entities: EntityNode[];
  focus: string | null;
  onPick: (id: string | null) => void;
}) {
  const list = entities.slice().sort((a, b) => b.idf - a.idf || b.mentions - a.mentions);
  return (
    <div className="rows">
      <div className="panel-note" style={{ padding: '0 0 6px' }}>
        按 idf 排：越往下越是「万物皆连」的枢纽噪声，已压暗。
      </div>
      {list.map((e) => {
        const on = focus === e.id;
        const dim = 0.34 + Math.min(0.66, e.idf * 1.1);
        return (
          <button key={e.id} className="row" aria-selected={on} style={{ opacity: on ? 1 : dim }}
            onClick={() => onPick(on ? null : e.id)}
            title={`idf ${e.idf.toFixed(2)}${e.idf < 0.38 ? '（枢纽噪声，压暗）' : ''}`}>
            <span className="row-top">
              <span className="row-name">{e.label}</span>
              <span className="tag">{group(e.mentions)}</span>
            </span>
            {e.aliases.length > 0 && (
              <span className="row-sub">也叫：{e.aliases.join('、')}</span>
            )}
            <span className="row-tags">
              <span className="tag">{KIND_FACE[e.kind] ?? e.kind}</span>
              <span className={`tag${e.idf < 0.38 ? '' : ' gold'}`}>idf {e.idf.toFixed(2)}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** 实体类别的中文说法。⚠️ 键必须与契约的 EntityNode.kind 一致。 */
const KIND_FACE: Record<string, string> = {
  person: '人', agent: '灯', project: '项目', path: '路径',
  term: '术语', org: '组织', time: '时刻', mem_ref: '记忆引用',
};
