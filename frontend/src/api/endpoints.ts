/* ================================================================
   照渊台 · 八端点收发形状（总导游图）

  ▍这是什么
  ① 干什么：把八个已定死的端点，一一对应成 Req/Res 类型
  ② 给谁看：写 mock 的人、接真后端的人
  ③ 依赖：contract.ts 的基础枚举

  ▍结构（顺序 = 契约里给的顺序，别重排，方便对账）
  GET  /api/cockpit/state        → CockpitState
  GET  /api/rooms               → RoomsRes
  GET  /api/room/{id}/context   → RoomContext
  POST /api/msg/send            → SendReq / SendRes
  POST /api/task/ack            → AckReq / AckRes
  POST /api/room/{id}/schedule  → ScheduleReq / ScheduleRes
  POST /api/widget              → WidgetReq / WidgetRes
  GET  /api/graph/memory        → MemoryGraph

  ▍修改铁律
  1. 【不】改字段名与语义。疑问写进 docs/接口对接说明.md 的「疑问」节。
  2. 所有响应都可能是 ApiFault（错误态）——调用侧必须处理，见 client.ts。
  3. 私密条目：content=null 且 redacted=true。【不】设计"前端自己藏"的分支，
     后端本来就不给正文（见 contract.ts 铁律 2）。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

import type {
  AgentId, Presence, LampState, MsgKind, MsgState,
  Visibility, Audience, Tier, EvoRel, SpeakMode,
} from './contract';

/* ---------- 通用外壳 ---------- */

/** 错误态统一形状。degraded=true 表示后端在、但某条腿降级了。 */
export interface ApiFault {
  ok: false;
  code: 'unreachable' | 'timeout' | 'server_error' | 'forbidden' | 'bad_shape';
  message: string;
  degraded?: boolean;
  /** 上次成功的时刻，用于"渊面结冰"时告诉库主看到的是多久前的水 */
  stale_at?: string;
}

/* ---------- 帖：五等帖统一形状 ---------- */

/**
 * 一条帖。★ 四个命运位是这套设计的地基 ★
 * persist    会不会永久进渊（入库）
 * notify     会不会打扰对方（把人叫起来）
 * needs_ack  要不要回执
 * audience   谁看得见：对席 pair / 合席 room
 * ⚠️ 这四位由 kind 推出（见 src/api/fates.ts 的 FATE 表），但契约把它们显式下发，
 *    以便后端将来允许覆盖（例如一条 msg 被库主手动标为入渊）。
 */
export interface Message {
  id: string;
  room_id: string;
  topic_id: string | null;
  kind: MsgKind;
  state: MsgState;
  from: AgentId | 'ryuuzaki';
  to: (AgentId | 'ryuuzaki')[];
  /** 私密条目此处为 null，配合 redacted=true */
  content: string | null;
  redacted?: boolean;
  visibility: Visibility;
  /** visibility=shared_with 时的定向名单 */
  shared_with?: AgentId[];
  persist: boolean;
  notify: boolean;
  needs_ack: boolean;
  audience: Audience;
  created_at: string;
  /** 对端被唤醒的时刻，null=还没醒 */
  woke_at?: string | null;
  /** 流式回传的增量片段（轮询时后端累积下发，前端做逐字呈现） */
  stream?: string;
  /** 回复帖 id */
  reply_to?: string | null;
  /** 唤醒等待上限（秒），超时降级为留帖 */
  deadline_s?: number;
  /** 入渊后对应的记忆 id，可回跳到渊里那颗点 */
  chunk_id?: string | null;
}

/* ---------- GET /api/cockpit/state ---------- */

/** 一盏灯 = 一个主体。能力不同、在线方式不同、响应速度不同。 */
export interface Agent {
  id: AgentId;
  /** 名（玄铁 / 小D / 江北 / Z指导） */
  name: string;
  /** 一句话人格，用于灯下题名，不是介绍文案 */
  epithet: string;
  presence: Presence;
  lamp: LampState;
  /** 在哪醒着：进程/终端/桌面/会话 —— 决定"能立刻回话"还是"只能留帖" */
  channel: string;
  /** 唤醒实测中位延迟（毫秒）。0 = 无需唤醒 */
  wake_p50_ms: number;
  /** 能不能立刻回话。false 时发帖 UI 会明确改成"留帖" */
  can_answer_now: boolean;
  unread: number;
  /** 该角色名下未回执的差帖数 */
  open_tasks: number;
  /** 它写在渊里的记忆条数（含私密，私密只计数） */
  memories: number;
  /** 其中私密条数——只显条数，不显内容 */
  private_count: number;
  /** 它自己的衰减钟速率（相对 1.0），体现"按每个观察者各自的时钟衰减" */
  clock_rate: number;
  last_seen: string;
}

/** 库的体征。数字是质感，不是卖点——UI 里它们服务于装置的运动，不单独堆砌。 */
export interface Vitals {
  total: number;
  raw: number;
  refined: number;
  domains: number;
  clocks: number;
  evo_edges: number;
  /** 三种演化关系各自条数 */
  evo_by_rel: Record<EvoRel, number>;
  relations: number;
  /** 最近一次蒸馏（raw→refined 上浮）时刻 */
  last_distill: string;
  /** 今日淘汰条数，喂给装置的"溶解"动作 */
  evicted_today: number;
  /** 检索命中精炼层的比率（0–1），落空则回落全量 */
  refined_hit_rate: number;
}

export interface CockpitState {
  ok: true;
  now: string;
  /** 活性灯：整台操作台的心跳，0–1 */
  liveness: number;
  agents: Agent[];
  vitals: Vitals;
  unread_total: number;
  task_counts: { open: number; waiting_ack: number; done_today: number };
  /** 最近消息（含各自的状态，驱动帖案与航迹） */
  recent: Message[];
  /** 某个 AI 主动推上来的格子（见 POST /api/widget） */
  widgets: Widget[];
}

/* ---------- POST /api/widget ---------- */

/** 主动推一格内容到首页。kind 决定它在渊边如何呈现。 */
export interface Widget {
  id: string;
  from: AgentId;
  kind: 'note' | 'metric' | 'ask' | 'dream';
  title: string;
  /** 私密 widget 同样只给条数不给正文 */
  body: string | null;
  redacted?: boolean;
  visibility: Visibility;
  created_at: string;
  /** 需要库主拍板时给出选项，点击回 POST /api/task/ack */
  actions?: { id: string; label: string }[];
}

export interface WidgetReq { from: AgentId; kind: Widget['kind']; title: string; body: string | null; visibility: Visibility }
export interface WidgetRes { ok: true; widget: Widget }

/* ---------- GET /api/rooms ---------- */

/** 一间房。单聊是隐式对席房（members 两人），群聊是合席房。 */
export interface Room {
  id: string;
  /** dm=对席 / group=合席 */
  type: 'dm' | 'group';
  title: string;
  members: (AgentId | 'ryuuzaki')[];
  speak_mode: SpeakMode;
  /** 本轮还没回话的人 —— 群聊最有信息量的一格 */
  pending_speakers: AgentId[];
  /** 当前轮次序号，0=还没开轮 */
  round: number;
  round_state: 'idle' | 'running' | 'waiting' | 'closed';
  unread: number;
  last_at: string;
  /** 房下面的话题 */
  topics: Topic[];
}

export interface Topic {
  id: string;
  title: string;
  msg_count: number;
  /** 该话题里有多少条是私密的（只给条数） */
  private_count: number;
  last_at: string;
}

export interface RoomsRes { ok: true; rooms: Room[] }

/* ---------- POST /api/rooms（创建房间）★ 新增端点（库主拍板 2026-09-02）---------- */

/**
 * 创建房间的请求。★ 区分两种形态（与 Room.type 一致）★
 * 对席 dm    ：库里主想跟某盏灯开新话题 —— 只要 to，members 自动补 [库主, to]
 * 合席 group ：一间房多盏灯 —— 给 members + 可选发言模式
 * ⚠️ 每条端点只做"建房"，开轮次用 POST /api/room/{id}/schedule（别混）。
 */
export interface CreateRoomReq {
  /** dm=对席 / group=合席 */
  type: 'dm' | 'group';
  /** 对席房：要跟谁聊 */
  to?: AgentId;
  /** 合席房：哪些灯在席（库主默认在席，无需写） */
  members?: AgentId[];
  /** 首话题标题（可选，缺省由后端起一个） */
  topic?: string;
  /** 合席房的可选发言模式；缺省由后端决定 */
  speak_mode?: SpeakMode;
}

/** 创建成功的返回：给一枚可用的 Room（含 id / topics / last_at）。 */
export interface CreateRoomRes { ok: true; room: Room }


/* ---------- GET /api/room/{id}/context ---------- */

export interface RoomContext {
  ok: true;
  room: Room;
  /** 按话题过滤后的最近 N 条 */
  topic_id: string | null;
  n: number;
  messages: Message[];
}

/* ---------- POST /api/msg/send ---------- */

export interface SendReq {
  room_id: string;
  topic_id?: string | null;
  to: AgentId[];
  kind: MsgKind;
  content: string;
  visibility: Visibility;
  shared_with?: AgentId[];
  /** 等唤醒的上限秒数；超时降级留帖 */
  deadline_s?: number;
}

/** 发出后先拿到一条 queued 帖，之后靠轮询 /api/cockpit/state 看它走链。 */
export interface SendRes { ok: true; message: Message }

/* ---------- POST /api/task/ack ---------- */

/** 差帖回执，同时是"卡片按钮点击"的回调端点（契约合并了这两件事）。 */
export interface AckReq {
  message_id: string;
  /** 卡片按钮的 action id；纯回执时可省 */
  action_id?: string;
  note?: string;
}
export interface AckRes { ok: true; message: Message }

/* ---------- POST /api/room/{id}/schedule ---------- */

export interface ScheduleReq {
  /** 不传则沿用房间当前 speak_mode */
  mode?: SpeakMode;
  /** inviteonly 时点名 */
  invite?: AgentId[];
}
export interface ScheduleRes {
  ok: true;
  room_id: string;
  round: number;
  mode: SpeakMode;
  /** 本轮排定的发言顺序 */
  order: AgentId[];
  pending_speakers: AgentId[];
}

/* ---------- GET /api/graph/memory ---------- */

/**
 * 实体关系图：把散落的称呼归一成同一实体（aliases），用共现建边，
 * 用逆文档频率压制枢纽噪声（edge.idf 越低=越是万物皆连的噪声枢纽）。
 */
export interface EntityNode {
  id: string;
  type: 'node_entity';
  label: string;
  /** 归一掉的别名，这是"把散落称呼归一"的证据 */
  aliases: string[];
  /** 提到它的记忆条数 */
  mentions: number;
  /** 逆文档频率，低=枢纽噪声，UI 据此压暗 */
  idf: number;
  kind: 'person' | 'agent' | 'project' | 'path' | 'term' | 'org' | 'time' | 'mem_ref';
}

export interface MemoryNode {
  id: string;
  type: 'node_memory';
  tier: Tier;
  owner: AgentId;
  visibility: Visibility;
  /** 私密：null */
  excerpt: string | null;
  redacted?: boolean;
  importance: number;
  /** 热度，命中会回写 */
  heat: number;
  /** 各观察者时钟下的剩余寿命（0–1），0=到期淘汰 */
  life: number;
  /** 被新事实取代而退役 */
  retired?: boolean;
  created_at: string;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  /** mentions=记忆提到实体；co=实体共现；evo=演化关系 */
  rel: 'mentions' | 'co' | EvoRel;
  weight: number;
  /** 共现边的逆文档频率权重 */
  idf?: number;
}

export interface MemoryGraph {
  ok: true;
  entities: EntityNode[];
  memories: MemoryNode[];
  edges: GraphEdge[];
  /** 角色之间的六组两两关系 */
  bonds: Bond[];
}

/** 两两关系：亲疏、信任、人情债。 */
export interface Bond {
  a: AgentId;
  b: AgentId;
  /** 亲疏 0–1 */
  affinity: number;
  /** 信任 0–1 */
  trust: number;
  /** 人情债：正=a 欠 b，负=b 欠 a */
  favor_debt: number;
  dm_count: number;
  /** 每周共同蒸馏出的"梦境"，私密的只给条数 */
  dreams: number;
  last_dm: string;
}

