/* ================================================================
   照渊台 · mock 引擎（总导游图）★ 本文件核心模块，改动前务必读懂 ★

  ▍这是什么
  ① 干什么：一个有状态的假后端。它不是"返回死数据"，而是按真实时序推进
     每一条帖走完命途（投出 → 唤醒 → 流式 → 回复/超时 → 入渊 → 蒸馏 → 命中 → 退役）
  ② 给谁看：调命途规则、加场景、接真后端时要删掉这层的人
  ③ 依赖：fixtures/*.json（形状逐字段等于契约）+ api/fates.ts 的命运表
  ④ 为什么要有状态：契约规定前端只轮询。若 mock 无状态，轮询就看不出任何变化，
     "说话即上工"这条最重要的动态根本演不出来。

  ▍★ 命途裁决表 ★（这是"结果随输入而不同"的唯一出处）
  收件人决定能不能叫醒：
     玄铁 常明     0.0s 起灯，必成
     小D  引火     4.5s 起灯，必成
     江北 檐下    15.2s 起灯，九成成
     Z指导 行灯   can_answer_now=false → 直接留帖，不走唤醒（叫不醒）
  重量决定命运：
     便帖/席上语/风讯  不入渊 → 链在"回复到达"就收束，水面化汽
     差帖              入渊 + 打扰 + 等回执 → 多两幕：回执、蒸馏
     凭帖              入渊 + 不打扰 → 直接落到精炼层（不必先沉底再上浮）
  可见性决定谁看得见：
     private     入渊后其他灯照不到它，帖案里封蜡
     shared      四盏灯都能命中
     shared_with 只有点名的灯能命中；没点名的灯在"被看见"这一幕会显示"照不到"

  ▍修改铁律
  1. 【不】在这里写"看起来像随机"的抖动。命途必须可复现：同样输入 → 同样结局，
     否则这条链就变成了动画表演，不是可玩的机制。唯一的随机是 luck（见 rollLuck），
     且它被记录在帖上，可回看。
  2. 时间推进【不】用 setTimeout 链。全部靠 tick(now) 纯函数式推进——
     这样切标签页回来不会漏帧，也能被"加速演练"直接跳时。
  3. 加一幕必须同时加进 ACTS 表与 stateOfAct()，缺一头 UI 就会卡在旧状态（注释与代码同批改）。

  ▍外号表
  "命途"   → Journey，一条帖从投出到消散的完整轨迹
  "幕"     → Act，命途的一段（唤醒 / 流式 / 回复 / 入渊 / 蒸馏 / 命中 / 退役）
  "裁决"   → verdict()，由收件人+重量+可见性算出这条帖会经历哪几幕
  "运"     → luck，唯一的随机数，写在帖上可回看

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

import type { AgentId, MsgKind, MsgState, Visibility } from '../api/contract';
import type { Agent, CockpitState, Message, Room, MemoryGraph, Bond, Topic } from '../api/endpoints';
import { FATE } from '../api/fates';

import rawState from '../../fixtures/cockpit-state.json';
import rawRooms from '../../fixtures/rooms.json';
import rawMsgs from '../../fixtures/messages.json';
import rawGraph from '../../fixtures/graph-memory.json';
import sceneEmpty from '../../fixtures/scenes/empty.json';
import sceneError from '../../fixtures/scenes/error.json';
import sceneDegraded from '../../fixtures/scenes/degraded.json';

/* ---------- 场景开关：地址栏 ?scene= 覆盖 ---------- */
export type Scene = 'normal' | 'empty' | 'error' | 'degraded';

/* ================================================================
   幕 — 命途的分段。每一幕都有"它在告诉我什么变了"的说法（says）
   ⚠️ 顺序即时序。ACTS 的 at_ms 是相对投出时刻的偏移。
   ================================================================ */
export type ActKind =
  | 'cast'       /* 投出：石子离手 */
  | 'ripple'     /* 涟漪爬向那盏灯 */
  | 'wake'       /* 灯爆芯 */
  | 'stream'     /* 火星在落：流式回传 */
  | 'reply'      /* 回声环回投点 */
  | 'timeout'    /* 叫不醒，转琥珀 */
  | 'sink'       /* 沉进全量层 */
  | 'ack'        /* 差帖回执，锚链解开 */
  | 'distill'    /* 被蒸馏，上浮到精炼层 */
  | 'hit'        /* 被检索命中，热度回写 */
  | 'seen'       /* 被另一盏灯看见（或因可见性照不到） */
  | 'superseded' /* 被新事实取代，翻面 */
  | 'evict';     /* 被时间带走，溶解成沙 */

export interface Act {
  kind: ActKind;
  /** 相对投出的毫秒偏移 */
  at_ms: number;
  /** 一句话：它在告诉我什么状态变了。UI 直接显示这句 */
  says: string;
  /** 这一幕涉及哪盏灯（唤醒 / 被看见 / 照不到） */
  who?: AgentId;
  /** 这一幕是不是"没发生"（例如可见性导致照不到） */
  negated?: boolean;
}

export interface Journey {
  msg_id: string;
  to: AgentId;
  kind: MsgKind;
  visibility: Visibility;
  shared_with: AgentId[];
  /** 投出的绝对时刻（ms） */
  t0: number;
  /** 唯一的随机：0–1，决定"江北那一成叫不醒"这类分叉。写在帖上可回看 */
  luck: number;
  acts: Act[];
  /** 命途总时长，UI 的时间轴据此定尺 */
  span_ms: number;
}

/* ================================================================
   裁决 verdict() — "结果随输入而不同"的唯一出处
   ================================================================ */

/** 四盏灯的唤醒特性。数字取自实测（见 fixtures 生成器注释）。 */
const WAKE: Record<AgentId, { ms: number; success: number; answers: boolean }> = {
  xuantie: { ms: 300, success: 1, answers: true },      /* 常明：几乎无延迟 */
  dsh: { ms: 4542, success: 0.97, answers: true },      /* 引火：要拉起来 */
  workbuddy: { ms: 15200, success: 0.9, answers: true },/* 檐下：桌面端，一成叫不醒 */
  zcode: { ms: 0, success: 0, answers: false },         /* 行灯：会话关了，只能留帖 */
};

/** 一条帖的等待上限。差帖给足 90 秒，其余 45 秒（与 fixtures 的 deadline_s 一致）。 */
const DEADLINE_MS: Record<MsgKind, number> = {
  msg: 45_000, chat: 45_000, task: 90_000, decision: 45_000, system: 45_000,
};

/**
 * verdict()：契约
 * 入参：to（收件人）/ kind（重量）/ visibility / shared_with / luck（0–1）/ agents（当下四灯状态）
 * 返回：Journey（不含 msg_id/t0，由调用方补）
 * 副作用：无（纯函数，同样输入必得同样结局 —— 见铁律 1）
 * 谁调用：MockBackend.send()，以及「递帖」界面的预演（未投出前就能看到命途草图）
 * 改动影响：⚠️ 改这里等于改整个作品的灵魂交互。加一幕必须同步 ACT 表与 stateOfAct()。
 */
export function verdict(args: {
  to: AgentId; kind: MsgKind; visibility: Visibility;
  shared_with?: AgentId[]; luck: number; agents: Agent[];
}): Omit<Journey, 'msg_id' | 't0'> {
  const { to, kind, visibility, luck } = args;
  const shared_with = args.shared_with ?? [];
  const f = FATE[kind];
  const w = WAKE[to];
  const live = args.agents.find((a) => a.id === to);
  /** 灯当下是否够得上：契约字段 can_answer_now 优先于静态表 */
  const reachable = (live?.can_answer_now ?? w.answers) && luck <= w.success;
  const acts: Act[] = [];
  const A = (kind: ActKind, at_ms: number, says: string, extra: Partial<Act> = {}) =>
    acts.push({ kind, at_ms, says, ...extra });

  A('cast', 0, `${f.label}离手，落进渊面。`);

  /* 涟漪：只有会打扰对方的帖才推得到灯下；不打扰的帖只是自己在水面散开 */
  const rippleMs = f.notify ? Math.max(600, w.ms * 0.35) : 800;
  A('ripple', rippleMs, f.notify
    ? `涟漪推到${nameOf(to)}的灯下。`
    : `涟漪自己散开——这一等不打扰任何人。`);

  if (!reachable) {
    /* 叫不醒：链在这里折角，降级留帖。内容不丢 */
    const t = f.notify ? Math.min(DEADLINE_MS[kind], w.answers ? w.ms * 3 + 4000 : 6000) : rippleMs + 600;
    A('timeout', t, w.answers
      ? `等满${(t / 1000).toFixed(0)}秒也没起灯——降级为留帖，内容不丢，水面转琥珀。`
      : `${nameOf(to)}这盏是行灯，会话关着——直接留帖，不走唤醒。`, { who: to });
    /* 留帖也照命运位入渊：差帖凭帖仍要落库，只是没人当场干活 */
    if (f.persist) {
      A('sink', t + 900, '虽然没人应，帖还是沉进了全量层——它是要作数的。');
      if (kind === 'decision') A('distill', t + 2200, '凭帖不必等蒸馏，落地即上浮到精炼层。');
    }
    return finish(acts, to, kind, visibility, shared_with, luck);
  }

  A('wake', rippleMs + Math.max(200, w.ms * 0.6), f.notify
    ? `${nameOf(to)}爆灯芯——被叫起来了。`
    : `${nameOf(to)}看见了，但没被惊动。`, { who: to });

  const streamAt = rippleMs + w.ms + 900;
  if (f.notify) A('stream', streamAt, '火星在落——她边干边回传。', { who: to });

  const replyAt = streamAt + (f.notify ? 2600 : 1200);
  A('reply', replyAt, '回声环回投点：回复到了。', { who: to });

  if (!f.persist) {
    A('evict', replyAt + 1600, `${f.label}不入渊——水面化汽，看过即散。`);
    return finish(acts, to, kind, visibility, shared_with, luck);
  }

  /* 入渊：差帖先沉底再蒸馏；凭帖落地即上浮 */
  if (kind === 'decision') {
    A('sink', replyAt + 700, '落上石阶。');
    A('distill', replyAt + 1900, '凭帖落地即镀金，直入精炼层——日后作数。');
  } else {
    A('sink', replyAt + 900, '沉进全量层，压在旧事下面。');
    if (f.needs_ack) A('ack', replyAt + 3400, '锚链解开——差帖回执到了，这活了结。');
    A('distill', replyAt + 5200, '本周蒸馏把它捞起来，上浮到精炼层。');
  }

  /* 被检索命中 → 热度回写 */
  A('hit', replyAt + 7200, '有人查到了它，热度回写一格。');

  /* 被另一盏灯看见（或因可见性照不到）—— 这是"可见性三态"的命途证据 */
  const others = (['xuantie', 'dsh', 'workbuddy', 'zcode'] as AgentId[]).filter((x) => x !== to);
  const watcher = others[Math.floor(luck * others.length) % others.length];
  if (visibility === 'private') {
    A('seen', replyAt + 8600, `${nameOf(watcher)}那边只看到一格封蜡：有东西，读不到字。`, { who: watcher, negated: true });
  } else if (visibility === 'shared_with') {
    const ok = shared_with.includes(watcher);
    A('seen', replyAt + 8600, ok
      ? `${nameOf(watcher)}在点名单里——蜡上那道缝正好照进去。`
      : `${nameOf(watcher)}不在点名单里，连形都看不到。`, { who: watcher, negated: !ok });
  } else {
    A('seen', replyAt + 8600, `${nameOf(watcher)}查库时照到了它——记忆是水，全楼通。`, { who: watcher });
  }

  /* 结局：被取代 / 被时间带走。由 luck 分叉，写在帖上可回看 */
  if (luck < 0.42) {
    A('superseded', replyAt + 11_200, '一条更新的事实指过来——它翻面退役了，但没被删。');
  } else {
    A('evict', replyAt + 12_400, `${nameOf(to)}那口钟走到了头——溶解成沙，落回渊底。`, { who: to });
  }
  return finish(acts, to, kind, visibility, shared_with, luck);
}

function finish(
  acts: Act[], to: AgentId, kind: MsgKind,
  visibility: Visibility, shared_with: AgentId[], luck: number,
): Omit<Journey, 'msg_id' | 't0'> {
  const span = acts.length ? acts[acts.length - 1].at_ms + 1200 : 1200;
  return { to, kind, visibility, shared_with, luck, acts, span_ms: span };
}

/** 名字表：四盏灯的中文名。⚠️ 与 fixtures 的 AGENTS.name 必须一致。 */
const NAMES: Record<AgentId, string> = {
  xuantie: '玄铁', dsh: '小D', workbuddy: '江北', zcode: 'Z指导',
};
export const nameOf = (id: AgentId | 'ryuuzaki'): string =>
  id === 'ryuuzaki' ? '龙崎' : NAMES[id];

/**
 * stateOfAct()：把"走到第几幕"翻译成契约里的 MsgState。
 * ⚠️ 加幕必须同步这里，否则 UI 会卡在旧状态（见铁律 3）。
 */
export function stateOfAct(k: ActKind): MsgState {
  switch (k) {
    case 'cast': return 'queued';
    case 'ripple': return 'queued';
    case 'wake': return 'waking';
    case 'stream': return 'streaming';
    case 'reply': return 'replied';
    case 'timeout': return 'timeout';
    case 'ack': return 'acked';
    default: return 'replied';
  }
}

/* ================================================================
   MockBackend — 有状态假后端
   ★ 它的全部对外接口就是八个端点，形状逐字段等于契约 ★
   ⚠️ 时间推进靠 tick(now)（铁律 2）：不用 setTimeout 链，切标签页回来不漏帧。
   ================================================================ */

/** 对端流式回传的话术池。按收件人分，让"谁在说话"能听出来。 */
const STREAM_LINES: Record<AgentId, string[]> = {
  xuantie: ['正在扫向量缓存……', '已比出两处口径差值，', '剩下的按空值归档；', '条数一会儿给你。'],
  dsh: ['先绕过那个幽灵 provider，', '拉起 headless……', '捞到 234 行残留，', '要不要我顺手清掉？'],
  workbuddy: ['我这边桌面刚醒，', '正在读 automations 表……', '今日淘汰 23 条，', '其中 9 条是被取代的。'],
  zcode: ['……（行灯没亮）'],
};

const REPLY_LINES: Record<AgentId, string> = {
  xuantie: '清完了：向量缓存 3,148 行里 2,914 行有归属，234 行 chunk_id 为空，等你判一次。',
  dsh: '拉起来了。残留 234 行我先隔离没删——删之前想让你看一眼。',
  workbuddy: '捞出来了：今日淘汰 23 条，被取代 9 条、钟走完 14 条。名单我推成一格给你。',
  zcode: '（留帖）行灯没亮，这条我先记着，会话一开就办。',
};

export class MockBackend {
  private scene: Scene;
  private state: CockpitState;
  private rooms: Room[];
  private msgs: Message[];
  private graph: MemoryGraph;
  /** 活着的命途。key=msg_id */
  private journeys = new Map<string, Journey>();
  /** 自增序号，保证 id 稳定可读 */
  private seq = 9000;
  /** 演练用：把时间往前拨（毫秒），让长命途不必真等 */
  private skew = 0;

  constructor(scene: Scene = 'normal') {
    this.scene = scene;
    const s = this.sceneData();
    this.state = structuredClone(s.state) as CockpitState;
    this.rooms = structuredClone(s.rooms) as Room[];
    this.msgs = structuredClone(s.msgs) as Message[];
    this.graph = structuredClone(s.graph) as MemoryGraph;
  }

  /** 场景数据源。error/degraded 的失败响应由各 get 方法按场景抛出。 */
  private sceneData() {
    if (this.scene === 'empty') {
      return {
        state: sceneEmpty['cockpit-state'], rooms: sceneEmpty.rooms.rooms,
        msgs: [] as unknown[], graph: sceneEmpty.graph,
      };
    }
    if (this.scene === 'degraded') {
      return {
        state: sceneDegraded['cockpit-state'], rooms: sceneDegraded.rooms.rooms,
        msgs: rawMsgs.messages.slice(0, 12), graph: rawGraph,
      };
    }
    return { state: rawState, rooms: rawRooms.rooms, msgs: rawMsgs.messages, graph: rawGraph };
  }

  /** 演练加速：把所有活着的命途往前拨。UI 的「快进」按钮用它。 */
  skip(ms: number) { this.skew += ms; }
  get now() { return Date.now() + this.skew; }

  /* ---------- 时间推进 ---------- */

  /**
   * tick()：把每条活着的帖推到它此刻该在的幕。
   * 副作用：改 this.msgs（状态/流式文本/chunk_id）、改 this.graph（沉底/上浮/热度/退役）
   * 谁调用：每个 get 端点入口（因为前端只轮询，轮询即推进）
   * 改动影响：⚠️ 这里是"轮询也能看出变化"的唯一来源，改动会直接影响链的可玩性。
   */
  private tick() {
    const now = this.now;
    for (const [id, j] of this.journeys) {
      const m = this.msgs.find((x) => x.id === id);
      if (!m) { this.journeys.delete(id); continue; }
      const el = now - j.t0;
      const done = j.acts.filter((a) => a.at_ms <= el);
      if (!done.length) continue;
      const last = done[done.length - 1];

      /* 状态推进：只在"链上的状态幕"更新，后续幕（入渊/蒸馏/退役）不覆盖终态 */
      const st = stateOfAct(last.kind);
      if (last.kind === 'ack') m.state = 'acked';
      else if (last.kind === 'timeout') m.state = 'timeout';
      else if (m.state !== 'acked' && m.state !== 'timeout') m.state = st;

      /* 流式回传：逐句累积，模拟 SSE 被轮询"追上"的观感 */
      if (done.some((a) => a.kind === 'stream')) {
        const lines = STREAM_LINES[j.to];
        const sAt = j.acts.find((a) => a.kind === 'stream')!.at_ms;
        const k = Math.min(lines.length, Math.floor((el - sAt) / 700) + 1);
        m.stream = lines.slice(0, Math.max(1, k)).join('');
      }
      if (done.some((a) => a.kind === 'wake')) m.woke_at = new Date(j.t0 + j.acts.find((a) => a.kind === 'wake')!.at_ms).toISOString();

      /* 回复：造一条回帖，落进往来记录 */
      if (done.some((a) => a.kind === 'reply') && !this.msgs.some((x) => x.reply_to === id)) {
        this.msgs.unshift(this.mkReply(m, j));
      }
      if (done.some((a) => a.kind === 'timeout') && !this.msgs.some((x) => x.reply_to === id)) {
        /* 超时也留一条系统风讯，说明降级——内容不丢这件事要看得见 */
        this.msgs.unshift(this.mkSystem(m, `叫不醒${nameOf(j.to)}，这条${FATE[j.kind].label}降级为留帖，内容留在时间线上。`));
      }

      /* 入渊 / 蒸馏 / 命中 / 退役：改图，让渊谱同步长出这颗点 */
      if (done.some((a) => a.kind === 'sink')) this.ensureChunk(m, j, done);
    }

    /* 灯随命途变化：有帖在唤醒/流式，对应的灯就该是 waking/working */
    for (const a of this.state.agents) {
      const mine = [...this.journeys.values()].filter((j) => j.to === a.id);
      if (!mine.length) continue;
      const el = (j: Journey) => now - j.t0;
      const streaming = mine.some((j) => j.acts.some((x) => x.kind === 'stream' && x.at_ms <= el(j))
        && !j.acts.some((x) => x.kind === 'reply' && x.at_ms <= el(j)));
      const waking = mine.some((j) => j.acts.some((x) => x.kind === 'ripple' && x.at_ms <= el(j))
        && !j.acts.some((x) => (x.kind === 'wake' || x.kind === 'timeout') && x.at_ms <= el(j)));
      if (streaming) a.lamp = 'working';
      else if (waking) a.lamp = 'waking';
      else if (a.can_answer_now) a.lamp = 'online';
    }
    this.state.now = new Date(now).toISOString();
  }

  private mkReply(src: Message, j: Journey): Message {
    return {
      id: `m-${++this.seq}`, room_id: src.room_id, topic_id: src.topic_id,
      kind: src.kind === 'task' ? 'msg' : src.kind === 'chat' ? 'chat' : 'msg',
      state: 'delivered', from: j.to, to: ['ryuuzaki'],
      content: REPLY_LINES[j.to], visibility: 'shared',
      persist: false, notify: false, needs_ack: false,
      audience: src.audience,
      created_at: new Date(this.now).toISOString(),
      woke_at: null, reply_to: src.id, deadline_s: 45, chunk_id: null,
    };
  }

  private mkSystem(src: Message, text: string): Message {
    return {
      id: `m-${++this.seq}`, room_id: src.room_id, topic_id: src.topic_id,
      kind: 'system', state: 'delivered', from: 'ryuuzaki', to: [],
      content: text, visibility: 'shared',
      persist: false, notify: false, needs_ack: false, audience: 'room',
      created_at: new Date(this.now).toISOString(),
      woke_at: null, reply_to: src.id, deadline_s: 45, chunk_id: null,
    };
  }

  /** 让这条帖在渊谱里真的长出一颗点，并按已走的幕更新它的层/热度/寿命。 */
  private ensureChunk(m: Message, j: Journey, done: Act[]) {
    let node = this.graph.memories.find((x) => x.id === m.chunk_id);
    if (!node) {
      const id = `c-new-${++this.seq}`;
      m.chunk_id = id;
      node = {
        id, type: 'node_memory', tier: 'raw', owner: j.to,
        visibility: j.visibility,
        excerpt: j.visibility === 'private' ? null : m.content,
        ...(j.visibility === 'private' ? { redacted: true } : {}),
        importance: j.kind === 'decision' ? 0.9 : 0.7,
        heat: 0.1, life: 1, created_at: new Date(this.now).toISOString(),
      };
      this.graph.memories.unshift(node);
      this.state.vitals.total += 1;
      this.state.vitals.raw += 1;
    }
    if (done.some((a) => a.kind === 'distill') && node.tier === 'raw') {
      node.tier = 'refined';
      this.state.vitals.raw -= 1;
      this.state.vitals.refined += 1;
      this.state.vitals.last_distill = new Date(this.now).toISOString();
    }
    if (done.some((a) => a.kind === 'hit')) node.heat = Math.min(1, node.heat + 0.35);
    if (done.some((a) => a.kind === 'superseded') && !node.retired) {
      node.retired = true; node.life = 0;
      this.state.vitals.evo_edges += 1;
      this.state.vitals.evo_by_rel.supersedes += 1;
      this.graph.edges.push({
        id: `g-new-${++this.seq}`,
        source: this.graph.memories.find((x) => x.id !== node!.id && x.tier === 'refined')?.id ?? node.id,
        target: node.id, rel: 'supersedes', weight: 1,
      });
    }
    if (done.some((a) => a.kind === 'evict') && node.life > 0) {
      node.life = 0;
      this.state.vitals.evicted_today += 1;
    }
  }

  /* ================================================================
     八个端点 — 形状逐字段等于契约
     ⚠️ error 场景下 get 类端点返回 ApiFault；degraded 场景只有 graph 这条腿断，
     其余照常返回——"部分腿降级"才是真实世界的样子，全断反而简单。
     ================================================================ */

  /** GET /api/cockpit/state */
  getState(): CockpitState | ApiFaultLike {
    if (this.scene === 'error') return sceneError['cockpit-state'] as ApiFaultLike;
    this.tick();
    return {
      ...this.state,
      /* 活性灯：有帖在链上走就往上顶，安静时回落——它是心跳不是装饰 */
      liveness: clamp(this.state.liveness * 0.55 + this.journeys.size * 0.22 + 0.18, 0, 1),
      unread_total: this.rooms.reduce((s, r) => s + r.unread, 0),
      task_counts: {
        open: this.msgs.filter((m) => m.kind === 'task' && m.state !== 'acked').length,
        waiting_ack: this.msgs.filter((m) => m.kind === 'task' && (m.state === 'replied' || m.state === 'delivered')).length,
        done_today: this.state.task_counts.done_today,
      },
      recent: this.msgs.slice(0, 24),
    };
  }

  /** GET /api/rooms */
  getRooms() {
    if (this.scene === 'error') return sceneError.rooms as ApiFaultLike;
    this.tick();
    return { ok: true as const, rooms: this.rooms };
  }

  /** POST /api/rooms —— 创建房间（对席/合席）★ 新增端点（库主拍板 2026-09-02）★
   *  做两件事：造一间房（放回 this.rooms）+ 给它开一个话题。开轮次走 schedule，不在这里。
   */
  createRoom(req: { type: 'dm' | 'group'; to?: AgentId; members?: AgentId[]; topic?: string; speak_mode?: SpeakModeLike }) {
    if (this.scene === 'error') return sceneError.rooms as ApiFaultLike;
    this.tick();
    const type = req.type;
    if (type === 'dm') {
      const to = req.to;
      if (!to || !NAMES[to]) {
        return { ok: false as const, code: 'bad_shape' as const, message: '对席房必须给一个在册的 to（谁跟你聊）' };
      }
      const room: Room = {
        id: `dm:${to}`,
        type: 'dm',
        title: `与${NAMES[to]}`,
        members: ['ryuuzaki', to],
        speak_mode: 'sequential',
        pending_speakers: [],
        round: 0,
        round_state: 'idle',
        unread: 0,
        last_at: new Date(this.now).toISOString(),
        topics: [this.mkTopic(req.topic ?? `关于${NAMES[to]}`)],
      };
      /* 已有同 id 房就不重复建，返回现有的（幂等） */
      const exist = this.rooms.find((r) => r.id === room.id);
      if (exist) return { ok: true as const, room: exist };
      this.rooms.push(room);
      return { ok: true as const, room };
    }
    /* 合席房 */
    const members = (req.members ?? []).filter((m): m is AgentId => !!NAMES[m]);
    if (!members.length) {
      return { ok: false as const, code: 'bad_shape' as const, message: '合席房至少给一个在册成员' };
    }
    const id = `gr-${++this.seq}`;
    const room: Room = {
      id,
      type: 'group',
      title: `群聊 · ${members.map((m) => NAMES[m]).join('、')}`,
      members: ['ryuuzaki', ...members],
      speak_mode: req.speak_mode ?? 'sequential',
      pending_speakers: [],
      round: 0,
      round_state: 'idle',
      unread: 0,
      last_at: new Date(this.now).toISOString(),
      topics: [this.mkTopic(req.topic ?? '开场')],
    };
    this.rooms.push(room);
    return { ok: true as const, room };
  }

  /** 造一个话题（轻量 helper，避免 createRoom 里手搓 Topic）。 */
  private mkTopic(title: string): Topic {
    return {
      id: `t-${++this.seq}`,
      title,
      msg_count: 0,
      private_count: 0,
      last_at: new Date(this.now).toISOString(),
    };
  }

  /** GET /api/room/{id}/context —— 按话题过滤的最近 N 条 */
  getRoomContext(roomId: string, topicId: string | null, n = 40) {
    if (this.scene === 'error') return sceneError.rooms as ApiFaultLike;
    this.tick();
    const room = this.rooms.find((r) => r.id === roomId);
    if (!room) return { ok: false as const, code: 'bad_shape' as const, message: `没有这间房：${roomId}` };
    const messages = this.msgs
      .filter((m) => m.room_id === roomId && (!topicId || m.topic_id === topicId))
      .slice(0, n);
    return { ok: true as const, room, topic_id: topicId, n, messages };
  }

  /** GET /api/graph/memory */
  getGraph() {
    if (this.scene === 'error') return sceneError.graph as ApiFaultLike;
    if (this.scene === 'degraded') return sceneDegraded.graph as ApiFaultLike;
    this.tick();
    return this.graph;
  }

  /**
   * POST /api/msg/send —— 发消息（触发对端上工）
   * ★ 这是灵魂交互的入口 ★ 返回一条 queued 帖并登记一条命途；
   * 之后前端靠轮询把它一幕一幕看完。
   */
  send(req: {
    room_id: string; topic_id?: string | null; to: AgentId[];
    kind: MsgKind; content: string; visibility: Visibility;
    shared_with?: AgentId[]; deadline_s?: number;
  }) {
    if (this.scene === 'error') return sceneError['cockpit-state'] as ApiFaultLike;
    this.tick();
    const f = FATE[req.kind];
    const to = req.to[0];
    if (!to) return { ok: false as const, code: 'bad_shape' as const, message: '没有收件人：至少点一盏灯。' };
    const sealed = req.visibility === 'private';
    const m: Message = {
      id: `m-${++this.seq}`,
      room_id: req.room_id, topic_id: req.topic_id ?? null,
      kind: req.kind, state: 'queued',
      from: 'ryuuzaki', to: req.to,
      /* ⚠️ 私密：假后端也不下发正文（contract.ts 铁律 2）。
         正文只留在命途的裁决里用于计算，不进响应。 */
      content: sealed ? null : req.content,
      ...(sealed ? { redacted: true } : {}),
      visibility: req.visibility,
      ...(req.visibility === 'shared_with' ? { shared_with: req.shared_with ?? [] } : {}),
      persist: f.persist, notify: f.notify, needs_ack: f.needs_ack, audience: f.audience,
      created_at: new Date(this.now).toISOString(),
      woke_at: null, reply_to: null,
      deadline_s: req.deadline_s ?? DEADLINE_MS[req.kind] / 1000,
      chunk_id: null,
    };
    this.msgs.unshift(m);

    const luck = this.rollLuck(m.id);
    const j = verdict({
      to, kind: req.kind, visibility: req.visibility,
      shared_with: req.shared_with, luck, agents: this.state.agents,
    });
    this.journeys.set(m.id, { ...j, msg_id: m.id, t0: this.now });

    const room = this.rooms.find((r) => r.id === req.room_id);
    if (room) room.last_at = m.created_at;
    return { ok: true as const, message: m };
  }

  /**
   * POST /api/task/ack —— 差帖回执，同时是卡片按钮点击的回调
   * ⚠️ 契约把这两件事合并在一个端点，所以 action_id 可选：有 action_id 是点卡片，
   *    没有就是纯回执。前端【不】另造端点。
   */
  ack(req: { message_id: string; action_id?: string; note?: string }) {
    if (this.scene === 'error') return sceneError['cockpit-state'] as ApiFaultLike;
    this.tick();
    const m = this.msgs.find((x) => x.id === req.message_id);
    if (!m) {
      /* 也可能点的是 widget 上的按钮：那时 message_id 传的是 widget id */
      const w = this.state.widgets.find((x) => x.id === req.message_id);
      if (!w) return { ok: false as const, code: 'bad_shape' as const, message: `找不到这条：${req.message_id}` };
      this.state.widgets = this.state.widgets.filter((x) => x.id !== w.id);
      const note = req.action_id ? w.actions?.find((a) => a.id === req.action_id)?.label : req.note;
      const decided: Message = {
        id: `m-${++this.seq}`, room_id: `dm:${w.from}`, topic_id: null,
        kind: 'decision', state: 'delivered', from: 'ryuuzaki', to: [w.from],
        content: `就「${w.title}」拍板：${note ?? '已处置'}`,
        visibility: 'shared',
        persist: true, notify: false, needs_ack: false, audience: 'room',
        created_at: new Date(this.now).toISOString(),
        woke_at: null, reply_to: null, deadline_s: 45, chunk_id: null,
      };
      this.msgs.unshift(decided);
      const luck = this.rollLuck(decided.id);
      const j = verdict({ to: w.from, kind: 'decision', visibility: 'shared', luck, agents: this.state.agents });
      this.journeys.set(decided.id, { ...j, msg_id: decided.id, t0: this.now });
      return { ok: true as const, message: decided };
    }
    m.state = 'acked';
    this.state.task_counts.done_today += 1;
    /* 让命途也跳到 ack 那一幕，链上要看得出锚链解开 */
    const j = this.journeys.get(m.id);
    if (j) {
      const at = j.acts.find((a) => a.kind === 'ack')?.at_ms;
      if (at !== undefined) j.t0 = Math.min(j.t0, this.now - at);
    }
    return { ok: true as const, message: m };
  }

  /** POST /api/room/{id}/schedule —— 触发一轮群聊调度 */
  schedule(roomId: string, req: { mode?: SpeakModeLike; invite?: AgentId[] }) {
    if (this.scene === 'error') return sceneError.rooms as ApiFaultLike;
    this.tick();
    const room = this.rooms.find((r) => r.id === roomId);
    if (!room) return { ok: false as const, code: 'bad_shape' as const, message: `没有这间房：${roomId}` };
    const mode = req.mode ?? room.speak_mode;
    const members = room.members.filter((x): x is AgentId => x !== 'ryuuzaki');
    let order: AgentId[];
    if (mode === 'inviteonly') {
      order = (req.invite ?? []).filter((x) => members.includes(x));
    } else if (mode === 'sequential') {
      order = members;
    } else {
      /* naturerandom：能立刻回话的排前面，行灯排最后——"自然"不等于均匀随机 */
      order = members.slice().sort((a, b) => {
        const A = this.state.agents.find((x) => x.id === a)!;
        const B = this.state.agents.find((x) => x.id === b)!;
        return (Number(B.can_answer_now) - Number(A.can_answer_now)) || (A.wake_p50_ms - B.wake_p50_ms);
      });
    }
    room.speak_mode = mode;
    room.round += 1;
    room.round_state = order.length ? 'running' : 'idle';
    room.pending_speakers = order.slice();
    room.last_at = new Date(this.now).toISOString();

    /* 排定即上工：给每位排到的灯投一条席上语，让他们真的开口 */
    for (const who of order) {
      const m: Message = {
        id: `m-${++this.seq}`, room_id: roomId, topic_id: room.topics[0]?.id ?? null,
        kind: 'chat', state: 'queued', from: 'ryuuzaki', to: [who],
        content: mode === 'inviteonly' ? `点名${nameOf(who)}：这一轮你先说。` : `第 ${room.round} 轮，按${modeName(mode)}轮到${nameOf(who)}。`,
        visibility: 'shared',
        persist: false, notify: true, needs_ack: false, audience: 'room',
        created_at: new Date(this.now).toISOString(),
        woke_at: null, reply_to: null, deadline_s: 45, chunk_id: null,
      };
      this.msgs.unshift(m);
      const luck = this.rollLuck(m.id);
      const j = verdict({ to: who, kind: 'chat', visibility: 'shared', luck, agents: this.state.agents });
      this.journeys.set(m.id, { ...j, msg_id: m.id, t0: this.now });
    }
    return { ok: true as const, room_id: roomId, round: room.round, mode, order, pending_speakers: room.pending_speakers };
  }

  /** POST /api/widget —— 某个 AI 主动推一格内容到驾驶舱首页 */
  pushWidget(req: { from: AgentId; kind: 'note' | 'metric' | 'ask' | 'dream'; title: string; body: string | null; visibility: Visibility }) {
    if (this.scene === 'error') return sceneError['cockpit-state'] as ApiFaultLike;
    this.tick();
    const sealed = req.visibility === 'private';
    const w = {
      id: `w-${++this.seq}`, from: req.from, kind: req.kind, title: req.title,
      body: sealed ? null : req.body,
      ...(sealed ? { redacted: true } : {}),
      visibility: req.visibility,
      created_at: new Date(this.now).toISOString(),
      ...(req.kind === 'ask' ? { actions: [{ id: 'a-yes', label: '照办' }, { id: 'a-no', label: '先搁着' }] } : {}),
    };
    this.state.widgets = [w, ...this.state.widgets].slice(0, 8);
    return { ok: true as const, widget: w };
  }

  /* ---------- 内部：可复现的 luck ---------- */

  /**
   * rollLuck()：由帖 id 派生的确定性随机（见铁律 1）。
   * 同一个 id 永远得到同一个 luck，所以命途可复现、可回看、可讲解。
   */
  private rollLuck(id: string): number {
    let h = 2166136261;
    for (let i = 0; i < id.length; i++) { h ^= id.charCodeAt(i); h = Math.imul(h, 16777619); }
    return ((h >>> 0) % 10000) / 10000;
  }

  /** 给 UI 看的命途（时间轴要画它）。 */
  journeyOf(id: string): Journey | undefined { return this.journeys.get(id); }
  allJourneys(): Journey[] { return [...this.journeys.values()]; }
}

type ApiFaultLike = { ok: false; code: string; message: string; degraded?: boolean; stale_at?: string };
type SpeakModeLike = 'sequential' | 'naturerandom' | 'inviteonly';

const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
export const modeName = (m: SpeakModeLike) =>
  m === 'sequential' ? '轮流' : m === 'naturerandom' ? '自然随机' : '点名邀约';

/** 六组两两关系的取用口（渊谱与灯间暗金丝都要它）。 */
export const bondsOf = (g: MemoryGraph): Bond[] => g.bonds;
