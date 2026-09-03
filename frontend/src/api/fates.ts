/* ================================================================
   照渊台 · 命运表（总导游图）★ 本文件核心模块，改动前务必读懂 ★

  ▍这是什么
  ① 干什么：把"五等帖"从『名字不同』翻译成『命运不同』——四个命运位
  ② 给谁看：所有渲染帖的地方（帖案 / 渊里的投物 / 差帖看板）
  ③ 依赖：contract.ts 的枚举
  ④ 为什么单独一个文件：五处 UI 都要问"这条帖会怎样"，答案必须只有一个出处

  ▍★ 核心命题 ★
  五等帖的差别【不】是标签颜色，是这四位：
    persist    会不会永久进渊（入库）
    notify     会不会打扰对方（把人叫起来）
    needs_ack  要不要回执
    audience   谁看得见（对席 pair / 合席 room）
  UI 画的是这四位派生出的「投物形态」（stone），不是 kind 的名字。
  换句话说：用户看形状就知道命运，不必读标签。

  ▍投物形态（渊里那一下）与命运的对应
  便帖 msg      卵石 pebble  —— 不沉底：水面化汽（只在当下被看见）
  席上语 chat    散石 spray   —— 涟漪铺满全房，同样化汽
  差帖 task     铁锚 anchor  —— 灯爆芯（打扰）+ 沉底但有链拴住水面（等回执）
  凭帖 decision 石印 seal    —— 不打扰，静静落上石阶，落地即镀金（直入精炼层）
  风讯 system   风讯 gust    —— 没有投物，从渊边刮来一道风痕，化汽

  ▍修改铁律
  1. 【不】在别处硬编码 kind→样式的映射。要问命运就 import 这里的 FATE。
     散落一份就会出现"帖案说会入库、渊里却没沉"这种自相矛盾（注释与代码同批改原则）。
  2. 后端下发的 persist/notify/needs_ack/audience 优先于本表推导（见 fateOf 注释）——
     契约允许后端覆盖，前端【不】拿本表去纠正后端。
  3. 改了本表必须同步 docs/设计说明.md 的「五等帖」节。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

import type { Audience, MsgKind, MsgState, Visibility } from './contract';
import type { Message } from './endpoints';

/** 渊里那一下的形态。见上文「投物形态」节。 */
export type StoneForm = 'pebble' | 'spray' | 'anchor' | 'seal' | 'gust';

export interface Fate {
  kind: MsgKind;
  /** 中文名，全站唯一出处 */
  label: string;
  /** 一句话命运，写"它会怎样"，【不】写"它叫什么" */
  destiny: string;
  persist: boolean;
  notify: boolean;
  needs_ack: boolean;
  audience: Audience;
  stone: StoneForm;
  /** 沉底深度 0–1（0=水面化汽，1=贴石阶）。驱动渊里的落点 */
  depth: number;
  /** 重量，驱动入水动静（溅幅 / 涟漪初速） */
  weight: number;
}

/* ================================================================
   FATE 表 — 五等帖的全部真相
   顺序 = 由轻到重，UI 的选择器按此顺序排，用户扫一遍就有"轻重"直觉。
   ================================================================ */
export const FATE: Record<MsgKind, Fate> = {
  msg: {
    kind: 'msg', label: '便帖',
    destiny: '看过即散。不入渊，不打扰，只有对席两人看见。',
    persist: false, notify: false, needs_ack: false, audience: 'pair',
    stone: 'pebble', depth: 0.06, weight: 0.35,
  },
  chat: {
    kind: 'chat', label: '席上语',
    destiny: '只在当下被看见。不入渊，合席都听得到，散场就没了。',
    persist: false, notify: false, needs_ack: false, audience: 'room',
    stone: 'spray', depth: 0.1, weight: 0.5,
  },
  task: {
    kind: 'task', label: '差帖',
    destiny: '会把人叫起来干活。入渊，要回执——没回执前一直拴在水面上。',
    persist: true, notify: true, needs_ack: true, audience: 'pair',
    stone: 'anchor', depth: 0.72, weight: 1,
  },
  decision: {
    kind: 'decision', label: '凭帖',
    destiny: '悄悄归档，永久。不打扰任何人，落地即入精炼层，日后作数。',
    persist: true, notify: false, needs_ack: false, audience: 'room',
    stone: 'seal', depth: 0.95, weight: 0.85,
  },
  system: {
    kind: 'system', label: '风讯',
    destiny: '渊自己说的话。不入渊，不打扰，谁在都看得见。',
    persist: false, notify: false, needs_ack: false, audience: 'room',
    stone: 'gust', depth: 0.02, weight: 0.2,
  },
};

/** 由轻到重的次序，UI 选择器与图例都用它，别自己再排一遍。 */
export const FATE_ORDER: MsgKind[] = ['msg', 'chat', 'task', 'decision', 'system'];

/**
 * fateOf()：契约
 * 入参：m（Message，可只给 kind 的部分帖）
 * 返回：Fate（四命运位 + 投物形态）
 * 副作用：无
 * 谁调用：帖案 / 渊装置 / 差帖看板 / 递帖预演
 * 改动影响：⚠️ 后端显式下发的四位优先——契约允许后端把一条 msg 标为入渊，
 *          前端必须服从，【不】拿本表覆盖它（见铁律 2）。
 */
export function fateOf(m: Pick<Message, 'kind'> & Partial<Message>): Fate {
  const base = FATE[m.kind];
  return {
    ...base,
    persist: m.persist ?? base.persist,
    notify: m.notify ?? base.notify,
    needs_ack: m.needs_ack ?? base.needs_ack,
    audience: m.audience ?? base.audience,
  };
}

/* ---------- 可见性：三态的话术与纪律 ---------- */

export interface VisibilityFace {
  key: Visibility;
  label: string;
  /** 一句话说清"谁看得见" */
  who: string;
  /** 是否封蜡：只显条数不显内容 */
  sealed: boolean;
}

/**
 * ⚠️ 封蜡纪律：private 的正文后端根本不下发（content=null / redacted=true）。
 *    UI 呈现为"看得见形、读不到字"的黑蜡+金线，【不】写"您无权查看"这类提示文案——
 *    提示文案是在替权限系统道歉；封蜡是让人一眼相信"这里有东西但不该我看"。
 */
export const VISIBILITY: Record<Visibility, VisibilityFace> = {
  private: { key: 'private', label: '私密', who: '只有写它的人自己。库主也只看得到条数。', sealed: true },
  shared: { key: 'shared', label: '共享', who: '四盏灯都照得到，跨角色可互查。', sealed: false },
  shared_with: { key: 'shared_with', label: '定向', who: '只照给点名的那几位，其余人连形都看不到。', sealed: false },
};

/* ---------- 帖的状态：链上的位置 ---------- */

/** 状态在链上的进度（0–1），驱动帖案的航迹长度与渊里的涟漪半径。 */
export const STATE_PROGRESS: Record<MsgState, number> = {
  queued: 0.08, waking: 0.28, streaming: 0.55,
  delivered: 0.7, replied: 0.9, acked: 1,
  timeout: 0.62, failed: 0.5,
};

/** 状态的中文与"它在告诉我什么变了"。动效必须能被这句话解释，解释不了就删掉。 */
export const STATE_FACE: Record<MsgState, { label: string; says: string }> = {
  queued: { label: '已投', says: '石子离手，还没碰到对端。' },
  waking: { label: '唤醒中', says: '涟漪到了灯下，灯正在爆芯。' },
  streaming: { label: '正在干活', says: '火星在落——对端边干边回传。' },
  delivered: { label: '已送达', says: '对端看见了，但还没开口。' },
  replied: { label: '已回复', says: '回声环回到了投点。' },
  acked: { label: '已回执', says: '锚链解开，差帖了结。' },
  timeout: { label: '叫不醒', says: '灯没起来，降级为留帖——内容不丢，转琥珀色。' },
  failed: { label: '通道故障', says: '不是对端不理你，是路断了。' },
};
