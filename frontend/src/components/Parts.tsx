/* ================================================================
   照渊台 · 台面零件（总导游图）

  ▍这是什么
  ① 干什么：四处都要用的小零件——灯龛、体征条、降级卡、封蜡行、全部动作面板
  ② 给谁看：改这些零件外观的人
  ③ 依赖：api/*（形状）、lib/agents.ts（枚举翻译）、lib/format.ts、ui/Glyphs.tsx

  ▍文件从上到下的结构
  LampNiche    灯龛：四盏灯一格一盏（名/称号/在线方式/能否立刻回话/唤醒延迟/未读/封数）
  VitalsBar    体征条：库的数字（这是质感，不是卖点 —— 小字、底部一行）
  FaultCard    降级/错误卡（★ 降级【不】是白屏：旧数据留着 + 断腿说明 + 上次成功时刻 ★）
  SealRow      封蜡行：私密条目的统一呈现（只给条数，不给正文）
  ActionSheet  「全部动作」面板：★ 鼠标优先的总入口 —— 每行点了就执行 ★

  ▍★ 未读为什么不画红点 ★
  未读只用两处表达：装置里的焰高、灯龛里的一行字。红点是让人焦虑的设计，
  而这台操作台每天开几十次（见 docs/设计说明.md「不做」第 1 条）。
  【不】在这里加角标计数。

  ▍修改铁律
  1. 零 Emoji（硬底线）。要符号用 ui/Glyphs.tsx 或内联 SVG。
  2. 数字一律 .num 类 + lib/format.group()，否则上下扫不齐。
  3. 名字/称号一律从 Agent 契约字段取，【不】在前端写死（见 lib/agents.ts 铁律 1）。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T2/T4）*
================================================================ */

import type { AgentId } from '../api/contract';
import type { Agent, ApiFault, Vitals } from '../api/endpoints';
import { LAMP_FACE, PRESENCE_FACE, inLampOrder } from '../lib/agents';
import { ACTIONS, type KeyAct } from '../lib/keys';
import { ago, clock, dur, group, pct } from '../lib/format';
import { IconLamp } from '../ui/Glyphs';

/* ================================================================
   灯龛 — 渊上方四格
   ⚠️ 点一盏灯 = 聚焦它：装置里其他灯与它们的记忆会压暗（"谁看不见谁"）。
      再点一次取消。这是全鼠标可达的主路径之一。
   ================================================================ */
export function LampNiche({ agents, focus, onFocus }: {
  agents: Agent[];
  focus: AgentId | null;
  onFocus: (id: AgentId | null) => void;
}) {
  const list = inLampOrder(agents);
  const now = Date.now();
  return (
    <div className="niche" role="group" aria-label="四盏灯">
      {list.map((a) => {
        const on = focus === a.id;
        const lamp = LAMP_FACE[a.lamp];
        const pres = PRESENCE_FACE[a.presence];
        /* 焰高条：未读的视觉呈现，与装置里的焰同源（见文件头） */
        const flameH = 6 + Math.min(18, a.unread * 3.2);
        return (
          <button
            key={a.id}
            className="lamp-card"
            aria-pressed={on}
            onClick={() => onFocus(on ? null : a.id)}
            title={`${a.name}｜${pres.says} ${lamp.says}（点一下只照这一盏）`}
          >
            <span className="lamp-name">
              <IconLamp size={14} style={{ opacity: a.can_answer_now ? 0.9 : 0.4 }} />
              {a.name}
              <span className="flame" style={{ height: flameH }} aria-hidden />
            </span>
            <span className="lamp-epithet">{a.epithet}</span>
            <dl className="lamp-grid">
              <dt>在线</dt><dd>{pres.label}</dd>
              <dt>焰</dt><dd>{lamp.label}</dd>
              <dt>唤醒</dt><dd>{a.wake_p50_ms ? dur(a.wake_p50_ms) : '即时'}</dd>
              <dt>未读</dt><dd>{a.unread}</dd>
              <dt>封</dt><dd>{a.private_count}</dd>
            </dl>
            <span className="lamp-say">
              {a.can_answer_now ? '能立刻回话' : '只能留帖'} · 见于 {ago(a.last_seen, now)}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/* ================================================================
   体征条 — 底部一行小字
   ★ 数字是质感不是卖点：小字、单行、可横向滚，【不】做成大仪表盘 ★
   ================================================================ */
export function VitalsBar({ v, liveness, now }: { v: Vitals; liveness: number; now: number }) {
  const items: [string, string][] = [
    ['全量', group(v.raw)],
    ['精炼', group(v.refined)],
    ['共', group(v.total)],
    ['域', String(v.domains)],
    ['钟', String(v.clocks)],
    ['演化', `${group(v.evo_edges)}（代 ${v.evo_by_rel.supersedes} · 争 ${v.evo_by_rel.disagrees_with} · 系 ${v.evo_by_rel.related}）`],
    ['关系', String(v.relations)],
    ['命中精炼', pct(v.refined_hit_rate, 1)],
    ['今日淘汰', String(v.evicted_today)],
    ['上次蒸馏', ago(v.last_distill, now)],
    ['活性', pct(liveness)],
  ];
  return (
    <div className="vitals" role="group" aria-label="库的体征">
      {items.map(([k, val], i) => (
        <span key={k} className="vital">
          {i > 0 && <i className="vital-sep" aria-hidden />}
          {k} <b>{val}</b>
        </span>
      ))}
    </div>
  );
}

/* ================================================================
   降级 / 错误卡
   ⚠️ 有旧数据 = 降级（琥珀，看的是旧水）；没有旧数据 = 硬错（朱）。
      两者文案不同：降级要给"上次成功时刻"，硬错要给"这条腿断在哪"。
   ================================================================ */
export function FaultCard({ fault, hasStale, what }: { fault: ApiFault; hasStale: boolean; what: string }) {
  return (
    <div className={`fault${hasStale ? '' : ' hard'}`} role="status">
      <b>{hasStale ? `${what}这条腿断了` : `${what}读不出来`}</b>
      <div>{fault.message}</div>
      {fault.stale_at && (
        <div>
          {hasStale ? '你看到的是' : '上次成功于'} <span className="num">{clock(fault.stale_at)}</span> 的水
          {hasStale ? '（页面照旧可用，只是不新鲜）' : ''}
        </div>
      )}
    </div>
  );
}

/* ================================================================
   封蜡行 — 私密条目的统一呈现
   ⚠️ 只给条数，不给正文；【不】写"您无权查看"（见 docs/设计说明.md 原则 5）。
      slit=定向共享：蜡上开一道缝，说明"只照给点名的那几位"。
   ================================================================ */
export function SealRow({ count, slit, label }: { count?: number; slit?: boolean; label?: string }) {
  return (
    <span className="seal" data-slit={slit ? '1' : undefined} role="img"
      aria-label={label ?? (count !== undefined ? `封蜡：${count} 条，只给条数` : '封蜡：只给条数')}>
      {count !== undefined && <span className="seal-count">封 {count} 条</span>}
    </span>
  );
}

/* ================================================================
   全部动作面板 — ★ 鼠标优先的总入口（库主拍板 Win 端）★
   每一行本身就是按钮：点了立刻执行；右边的 kbd 只是"顺便告诉你有快捷键"。
   ⚠️ 内容全部来自 lib/keys.ts 的 ACTIONS 表，【不】在这里另列一份（否则会说谎）。
   ================================================================ */
export function ActionSheet({ onAct, onClose }: { onAct: (a: KeyAct) => void; onClose: () => void }) {
  const groups: ActionRowGroup[] = ['视图', '动作', '演练'];
  return (
    <div className="scrim" role="dialog" aria-modal="true" aria-label="全部动作"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="sheet">
        <div className="corner tl" /><div className="corner tr" />
        <div className="corner bl" /><div className="corner br" />
        <div className="sheet-head">
          <div>
            <div className="panel-title">全部动作</div>
            <div className="panel-note">点一行就执行；括号里的键是加速器，不是唯一入口</div>
          </div>
          <button className="btn" onClick={onClose} title="关闭">关闭<span className="btn-key">Esc</span></button>
        </div>
        <div className="sheet-body">
          {groups.map((g) => (
            <div key={g}>
              <div className="group-title">{g}</div>
              <div className="rows">
                {ACTIONS.filter((r) => r.group === g).map((r) => (
                  <button key={r.keys + r.says} className="act-row" onClick={() => { onAct(r.act); onClose(); }}>
                    <span>{r.says}</span>
                    {r.keys && <kbd>{r.keys}</kbd>}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

type ActionRowGroup = '视图' | '动作' | '演练';
