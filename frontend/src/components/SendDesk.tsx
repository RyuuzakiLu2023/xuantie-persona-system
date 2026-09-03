/* ================================================================
   照渊台 · 递帖台（总导游图）★ 全鼠标可完成，只有正文要打字 ★

  ▍这是什么
  ① 干什么：递帖视图的右栏表单——收件人 / 重量（五等帖）/ 可见性 / 正文 / 投出，
     以及投出前的「预演」：还没投就告诉你这枚帖会怎样
  ② 给谁看：改递帖流程、加字段的人
  ③ 依赖：api/fates.ts（命运表）、mock/engine.ts 的 verdict（预演）、lib/agents.ts

  ▍★ 鼠标优先（库主拍板 2026-09-02：Win 端）★
  收件人点灯卡、重量点帖卡、可见性点三段、定向勾灯卡、投出点按钮——
  全程零打字，唯一需要键盘的是正文那一格。
  ⚠️ 因此【不】允许把任何选择做成"输入 id"或"下拉里打字筛选"的形式。

  ▍★ 预演为什么重要 ★
  五等帖的差别是命运不是名字。若投出后才知道"这条会把人叫起来"，
  库主就得靠记忆去猜。预演把裁决（verdict）提前跑一遍，把幕数、
  会不会爆芯、能不能叫醒、谁看得见，投出前就摊开（见 docs/设计说明.md 动线一）。

  ▍文件从上到下的结构
  Props         受控字段全部提在 App（切视图不丢草稿 —— 见 App 的 draft 状态）
  SendDesk      五个 field + 预演条 + 投出钮

  ▍修改铁律
  1. 【不】在这里调 api.send()。投出由 App 统一处理（它还要开命途、驱动装置）。
  2. 可见性 private 时【不】提示"对方看不到"这类废话：封蜡本身就是话（原则 5）。
  3. 收件人只能单选。契约的 SendReq.to 是数组，但 mock 与命途按"一枚帖一盏灯"裁决
     （见 mock verdict 的 to）——要改成多选必须先在 docs/接口对接说明.md 挂疑问。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T2）*
================================================================ */

import type { AgentId, MsgKind, Visibility } from '../api/contract';
import type { Agent, Room } from '../api/endpoints';
import { FATE, FATE_ORDER, VISIBILITY, fateOf } from '../api/fates';
import { inLampOrder } from '../lib/agents';
import { verdict } from '../mock/engine';
import { dur } from '../lib/format';
import { FateBits } from './MsgRow';

export interface Draft {
  to: AgentId | null;
  kind: MsgKind;
  visibility: Visibility;
  sharedWith: AgentId[];
  text: string;
}

interface Props {
  agents: Agent[];
  room: Room | null;
  draft: Draft;
  /**
   * ⚠️ 只提交"改哪几个字段"，【不】提交整份 draft（见 错题本.md #4）。
   * 血泪：早先签名是 (d: Draft) => void，本组件用 { ...draft, ...patch } 自己拼。
   * 同一批次里连点两下（点收件人紧接着点帖等）两个处理函数读到的都是
   * 同一份还没重渲染的旧 draft，后一个把前一个的改动覆盖掉 —— 收件人凭空丢了。
   * 交给 App 用 setDraft(d => ...) 函数式合并，才保证每次都基于最新值。
   */
  onPatch: (patch: Partial<Draft>) => void;
  onCast: () => void;
  /** 正文框的 ref 挂在 App，n 键/按钮要把光标送进去 */
  textRef: React.RefObject<HTMLTextAreaElement | null>;
  busy: boolean;
}

export function SendDesk({ agents, room, draft, onPatch, onCast, textRef, busy }: Props) {
  const lamps = inLampOrder(agents);
  const target = draft.to ? agents.find((a) => a.id === draft.to) ?? null : null;
  const f = FATE[draft.kind];
  /* 只报"改了什么"，合并交给 App（见 Props.onPatch 的血泪注释） */
  const set = (patch: Partial<Draft>) => onPatch(patch);

  /* ---------- 预演：把裁决提前跑一遍（见文件头）----------
     ⚠️ luck 固定用 0.5：预演给"典型结局"，【不】预言那一成的运气分叉——
        否则库主会以为界面在承诺结果。真结局由投出时的 id 派生（mock rollLuck）。 */
  const preview = target
    ? verdict({
        to: target.id, kind: draft.kind, visibility: draft.visibility,
        shared_with: draft.sharedWith, luck: 0.5, agents,
      })
    : null;
  const willTimeout = preview?.acts.some((a) => a.kind === 'timeout') ?? false;
  const canCast = !!draft.to && draft.text.trim().length > 0 && !busy;

  return (
    <div className="desk">
      {/* ---------- 收件人：点灯卡 ---------- */}
      <div className="field">
        <div className="field-head">
          <span className="field-label">递给谁</span>
          <span className="field-hint">点一盏灯</span>
        </div>
        <div className="pick-lamps" role="group" aria-label="收件人">
          {lamps.map((a) => (
            <button
              key={a.id}
              className="pick-lamp"
              aria-pressed={draft.to === a.id}
              onClick={() => set({ to: a.id })}
              title={`${a.epithet}｜${a.can_answer_now ? '能立刻回话' : '只能留帖'}，唤醒 ${a.wake_p50_ms ? dur(a.wake_p50_ms) : '即时'}`}
            >
              <b>{a.name}</b>
              <small>{a.can_answer_now ? '能立刻回话' : '只能留帖'}</small>
              <small>{a.wake_p50_ms ? `唤醒 ${dur(a.wake_p50_ms)}` : '唤醒即时'}</small>
            </button>
          ))}
        </div>
      </div>

      {/* ---------- 重量：五等帖，命运位图形化 ---------- */}
      <div className="field">
        <div className="field-head">
          <span className="field-label">多重</span>
          <span className="field-hint">四个格子亮几个，就是它的命运</span>
        </div>
        <div className="pick-fates" role="group" aria-label="五等帖">
          {FATE_ORDER.map((k) => {
            const ff = FATE[k];
            return (
              <button
                key={k}
                className="pick-fate"
                aria-pressed={draft.kind === k}
                onClick={() => set({ kind: k })}
                title={ff.destiny}
              >
                <b>{ff.label}</b>
                {/* ff 本身就带 kind 与四命运位，无须再拼一层（拼了会重复覆盖 kind） */}
                <FateBits m={ff} />
                <small style={{ fontSize: 10, color: 'var(--ink-2)', lineHeight: 1.45 }}>
                  {ff.destiny.split('。')[0]}
                </small>
              </button>
            );
          })}
        </div>
      </div>

      {/* ---------- 可见性：三段 + 定向勾人 ---------- */}
      <div className="field">
        <div className="field-head">
          <span className="field-label">谁看得见</span>
          <span className="field-hint">{VISIBILITY[draft.visibility].who}</span>
        </div>
        <div className="seg" role="group" aria-label="可见性">
          {(['private', 'shared', 'shared_with'] as Visibility[]).map((v) => (
            <button key={v} aria-pressed={draft.visibility === v}
              onClick={() => set({ visibility: v })} title={VISIBILITY[v].who}>
              {VISIBILITY[v].label}
            </button>
          ))}
        </div>
        {draft.visibility === 'shared_with' && (
          <div className="pick-lamps" role="group" aria-label="点名单">
            {lamps.filter((a) => a.id !== draft.to).map((a) => {
              const on = draft.sharedWith.includes(a.id);
              return (
                <button key={a.id} className="pick-lamp" aria-pressed={on}
                  onClick={() => set({
                    sharedWith: on ? draft.sharedWith.filter((x) => x !== a.id) : [...draft.sharedWith, a.id],
                  })}
                  /* ⚠️ 这一格是勾选：值必须基于当次 draft.sharedWith 算，
                     但提交的仍是补丁 —— 合并由 App 做（见 Props.onPatch） */
                  title={on ? `${a.name}在单里：蜡上那道缝照给她` : `${a.name}不在单里：连形都看不到`}>
                  <b>{a.name}</b>
                  <small>{on ? '在单里' : '不在单里'}</small>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* ---------- 正文：唯一要打字的地方 ---------- */}
      <div className="field">
        <div className="field-head">
          <span className="field-label">写什么</span>
          <span className="field-hint">Ctrl+Enter 投出</span>
        </div>
        <textarea
          ref={textRef}
          className="body-input"
          value={draft.text}
          placeholder={room ? `投进「${room.title}」` : '先在左栏点一间房'}
          onChange={(e) => set({ text: e.target.value })}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && canCast) onCast(); }}
        />
      </div>

      {/* ---------- 预演条：投出前就看得到命途草图 ---------- */}
      {preview && target && (
        <div className="preview" role="status">
          <span className="lead">预演</span>：这枚{f.label}
          {f.notify
            ? willTimeout
              ? `会去敲${target.name}的灯，但那盏${target.can_answer_now ? '未必起得来' : '是行灯——直接留帖'}。`
              : `会把${target.name}叫起来干活。`
            : '不打扰任何人，只是自己在水面散开。'}
          {f.persist ? `它会沉进渊里${draft.kind === 'decision' ? '，落地即上浮到精炼层' : ''}。` : '它不入渊，看过即散。'}
          {f.needs_ack ? '拴一条锚链在水面，回执才解开。' : ''}
          {draft.visibility === 'private'
            ? '入渊后其他灯只看到一格封蜡。'
            : draft.visibility === 'shared_with'
              ? `只有点名的 ${draft.sharedWith.length} 位照得进去。`
              : '四盏灯都照得到。'}
          <div className="panel-note" style={{ marginTop: 4 }}>
            共 <span className="num">{preview.acts.length}</span> 幕 ·
            约 <span className="num">{(preview.span_ms / 1000).toFixed(1)}</span> 秒走完
          </div>
        </div>
      )}

      {/* ---------- 投出 ---------- */}
      <div className="field-head">
        <button className="btn-cast" onClick={onCast} disabled={!canCast}
          title={canCast ? '投出（Ctrl+Enter）' : '先点一盏灯，再写一句'}>
          {target && !fateOf({ kind: draft.kind }).notify ? '投下' : target?.can_answer_now === false ? '留帖' : '投下'}
        </button>
        <span className="field-hint">
          {draft.to ? '' : '还没点灯 · '}
          {draft.text.trim() ? '' : '正文还空着'}
        </span>
      </div>
    </div>
  );
}
