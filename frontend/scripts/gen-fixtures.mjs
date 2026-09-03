/* ================================================================
   照渊台 · fixtures 生成器（总导游图）

  ▍这是什么
  ① 干什么：确定性地生成 fixtures/*.json（假数据），跑一次就固化，不在运行时造数
  ② 技术栈：纯 Node ESM，零依赖
  ③ 给谁看：要调数据量级、加场景、改文案质感的人
  ④ 怎么跑：node scripts/gen-fixtures.mjs

  ▍结构
  rng            固定种子的 PRNG —— 每次生成结果必须一模一样，否则截图和记忆都会漂
  AGENTS/DOMAINS 四盏灯与五个域的基本盘
  POOL           记忆正文池：用项目真实词汇（蒸馏/可见性/唤醒桥/A2A…）撑质感
  build*()       八个端点各自的数据
  scenes/        空态、错误态、降级态三份覆盖数据

  ▍修改铁律
  1. 【不】改 SEED。改了种子等于把所有既有截图、文档里引用的条目 id 全部作废。
     要加数据就在池子末尾追加，别插在中间。
  2. 生成的 JSON 必须逐字段等于 src/api/endpoints.ts 的类型。
     加字段先改类型再改这里——反过来做，前端会静默拿到 undefined。
  3. 私密条目一律 content=null + redacted=true。
     ⚠️ 这里就是"后端不下发正文"这条纪律的落地点：假后端也必须守（见 contract.ts 铁律 2）。
     生成器里【不】允许出现"先写正文再让前端藏起来"的写法。

  ▍已知问题
  ⚠️ TODO（2026-09-02）：/api/graph/memory 目前返回的是「窗口」而非全库
  （260 条记忆节点 vs vitals.total 3214）。真后端若一次给全量，前端要加分页——
  这条疑问已写进 docs/接口对接说明.md 的「疑问」节。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const OUT = join(ROOT, 'fixtures');

/* ---------- 固定种子 PRNG（mulberry32）---------- */
const SEED = 0x5eed_2026;
let _s = SEED;
const rnd = () => {
  _s |= 0; _s = (_s + 0x6d2b79f5) | 0;
  let t = Math.imul(_s ^ (_s >>> 15), 1 | _s);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
const pick = (a) => a[Math.floor(rnd() * a.length)];
const ri = (a, b) => a + Math.floor(rnd() * (b - a + 1));
const rf = (a, b, d = 2) => +(a + rnd() * (b - a)).toFixed(d);
const shuffle = (a) => { const x = a.slice(); for (let i = x.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [x[i], x[j]] = [x[j], x[i]]; } return x; };

/* 基准时刻：固定，保证生成结果稳定。前端把它当"库存快照时间"用相对时间渲染 */
const NOW = new Date('2026-09-02T09:40:00+08:00');
const iso = (d) => new Date(d).toISOString();
const ago = (min) => iso(NOW.getTime() - min * 60_000);

/* ================================================================
   四盏灯 — 能力不同、在线方式不同、响应速度不同
   wake_p50_ms 取自实测（见公共库 mem-9c71f0c7）：
   玄铁 A2A 常驻即答 / 小D headless 4542ms / 江北 automations p50 15.2s / zcode 会话内
   ================================================================ */
const AGENTS = [
  {
    id: 'xuantie', name: '玄铁', epithet: '常明 · 铁砧上的火',
    presence: 'resident', channel: 'A2A 入站 · 常驻进程 9900',
    wake_p50_ms: 0, can_answer_now: true, clock_rate: 1.0,
  },
  {
    id: 'dsh', name: '小D', epithet: '引火 · 鱼塘的水影',
    presence: 'on_demand', channel: 'headless 拉起 · 端口 28000',
    wake_p50_ms: 4542, can_answer_now: true, clock_rate: 0.72,
  },
  {
    id: 'workbuddy', name: '江北', epithet: '檐下 · 茶馆的逗哏',
    presence: 'desktop', channel: '桌面端 automations 表驱动',
    wake_p50_ms: 15200, can_answer_now: true, clock_rate: 1.35,
  },
  {
    id: 'zcode', name: 'Z指导', epithet: '行灯 · 命令行里的发布会',
    presence: 'session', channel: '会话内 MCP · 关了只能留帖',
    wake_p50_ms: 30000, can_answer_now: false, clock_rate: 1.1,
  },
];
const AIDS = AGENTS.map((a) => a.id);
const DOMAINS = ['公域', '玄铁域', '小D域', '江北域', 'Z指导域'];

/* ================================================================
   记忆正文池 — 用项目真实词汇撑质感，不写"示例文本 1"这种废话
   ================================================================ */
const POOL = [
  '可见性只覆盖 chunks 一张表，向量缓存里的正文没跟着走——补一条 min_visibility 统一实现',
  '衍生记忆必须带 source_ids 血缘，取 min(所有来源) 作为它自己的可见性',
  '蒸馏是把同一件事的七八种说法合成一句，不是摘要——摘要会把"谁说的"丢掉',
  '检索先问精炼层，落空才回落全量；命中要回写热度，不然热的会慢慢凉掉',
  '每个观察者一口钟：同一条记忆在江北那儿三天就凉，在玄铁那儿能撑两周',
  '同域新事取代旧事叫「代」，跨域视角分歧叫「争」——争不许自动取代',
  '别名归一：把「龙崎」「库主」「Ryuuzaki」认成同一个实体，共现才算得准',
  '逆文档频率压枢纽：万物皆连的那个词权重要压下去，否则整张图被它拽成一团',
  '唤醒桥实测三端都在六十秒内，"必须异步"这条假设当场作废',
  '玄铁的 A2A 插件是现成的，只是默认关着——启用比重写省十天',
  '手动 cron run 会抢 fire claim，自然触发的结果反被判 stale 整场丢弃',
  'deliver=local 的任务从不发卡片，拦截日志有行不等于发出去了',
  'importance 没有限幅，一条命令就能把 top-1 钉死——两行 clamp 最先做',
  '消息线不是记忆线：便帖席上语只进时间线，差帖凭帖才落库',
  '群聊调度在客户端不在服务端，拓扑跟我们正好相反，存储别照搬',
  '私密反思不进共享层，但它会悄悄改变下次打招呼的语气',
  '把「四端」写成四个 API 端点是最省事也最错的抽象',
  '梦境是两盏灯每周共同蒸馏一次的产物，署名是两个人',
  '人情债是有方向的：这周谁替谁顶了班，下周排活要算进去',
  '淘汰不等于错，只是这口钟走到了头；被取代才是判它错',
  '一条记忆的寿命环走完之前，热度每被命中一次就往回拨一格',
  '域隔离是真的，可见性是补的——这两件事别混着说',
  '轮次里没回话的人比回了话的人更有信息量',
  '定向共享的蜡上要开一道缝，不然被点名的人自己也看不见',
  '同一条事实被两个人各写一遍，图上会多出一条没用的共现边',
  '存档不是备份：存档要能被检索，备份只要能被还原',
  '灯灭了不代表人走了，可能只是会话关了',
  '「正在干活」这四个字是流式回传的第一个信号，比进度条实在',
];
const PRIVATE_POOL_COUNT = 6; /* 池子里被标为私密的条数占比由下方 visibility 分配控制 */

/* ================================================================
   十条代谢钟 — 两层库 × 五个域，各自一口钟
   ================================================================ */
const CLOCKS = [];
for (const tier of ['raw', 'refined']) {
  for (const d of DOMAINS) {
    CLOCKS.push({ tier, domain: d, half_life_d: tier === 'raw' ? ri(4, 12) : ri(21, 90) });
  }
}

const uid = (p, n) => `${p}-${String(n).padStart(4, '0')}`;
mkdirSync(OUT, { recursive: true });
mkdirSync(join(OUT, 'scenes'), { recursive: true });

/* ================================================================
   帖：五等帖 — 命运位必须和 src/api/fates.ts 的 FATE 表一致
   ⚠️ 这里是唯一的"假后端"，它下发的四位就是前端渲染依据；
   写错一位，UI 就会画出"说会入库却没沉底"的自相矛盾。
   ================================================================ */
const FATE = {
  msg: { persist: false, notify: false, needs_ack: false, audience: 'pair' },
  chat: { persist: false, notify: false, needs_ack: false, audience: 'room' },
  task: { persist: true, notify: true, needs_ack: true, audience: 'pair' },
  decision: { persist: true, notify: false, needs_ack: false, audience: 'room' },
  system: { persist: false, notify: false, needs_ack: false, audience: 'room' },
};

const MSG_TEXT = {
  msg: [
    '那条 clamp 我先加上了，你回头看一眼阈值合不合适',
    '昨晚那份对账我看完了，第三节的口径跟我这边差一格',
    '你要的实测数字在我这儿，等下贴给你',
    '别改那个文件，我正拿它跑回归',
  ],
  chat: [
    '这轮我先说：靶点应该是图边那张表，不是关系表',
    '同意上面的，但"争"这种关系不能自动取代，得留两条',
    '我补一句——枢纽词压不下去，整张图会被拽成一团',
    '那就先按容量约束做，权重惩罚已经被证伪了',
  ],
  task: [
    '把私有正文的三份无保护副本清一遍，先从向量缓存开始，做完给我条数',
    '启用 A2A 插件并跑通入站，跑通后把 agent card 的 skills 列表贴上来',
    '给检索链补上血缘过滤，四处边界各查一次权限，做完写一条凭帖',
    '把今天淘汰的那批捞出来，看有多少是被取代的、多少是钟走完的',
  ],
  decision: [
    '定了：消息线与记忆线分开——便帖席上语只进时间线，差帖凭帖才落库',
    '定了：四端主协议走 A2A，唤醒桥降级兜底，不自建 WebSocket',
    '定了：渊谱用确定性布局，不用力导向——空间记忆比好看重要',
    '定了：私密条目一律后端不下发正文，前端连藏的分支都不写',
  ],
  system: [
    '本周蒸馏完成：上浮 41 条，退役 12 条',
    '玄铁灯焰稳定，A2A 入站延迟中位 0 毫秒',
    'Z指导会话已关闭，转为留帖模式',
    '今日淘汰 23 条，其中 9 条为被取代退役',
  ],
};

/* ================================================================
   房间 — 单聊四间（隐式对席房）+ 群聊两间（合席房）
   房下面有话题；群聊有三种发言调度与"还没回话的人"
   ================================================================ */
const ROOM_TOPICS = {
  'dm:xuantie': [['t-xt-1', '可见性补漏'], ['t-xt-2', 'A2A 启用']],
  'dm:dsh': [['t-ds-1', '幽灵 provider'], ['t-ds-2', 'webServer 插件']],
  'dm:workbuddy': [['t-wb-1', 'automations 通道']],
  'dm:zcode': [['t-zc-1', '审计复盘'], ['t-zc-2', '驾驶舱前端']],
  'g:round': [['t-g1-1', '记忆分层与蒸馏'], ['t-g1-2', '演化关系三分'], ['t-g1-3', '枢纽噪声压制']],
  'g:hall': [['t-g2-1', '本周梦境'], ['t-g2-2', '人情债结算']],
};

function buildRooms() {
  const rooms = [];
  for (const a of AGENTS) {
    const id = `dm:${a.id}`;
    rooms.push({
      id, type: 'dm', title: `与${a.name}`,
      members: ['ryuuzaki', a.id],
      speak_mode: 'sequential', pending_speakers: [],
      round: 0, round_state: 'idle',
      unread: a.id === 'xuantie' ? 3 : a.id === 'zcode' ? 1 : 0,
      last_at: ago(ri(4, 300)),
      topics: ROOM_TOPICS[id].map(([tid, title], i) => ({
        id: tid, title, msg_count: ri(6, 48),
        private_count: i === 0 ? ri(0, 3) : 0, last_at: ago(ri(5, 600)),
      })),
    });
  }
  rooms.push({
    id: 'g:round', type: 'group', title: '圆桌 · 记忆分层',
    members: ['ryuuzaki', ...AIDS],
    speak_mode: 'naturerandom',
    pending_speakers: ['workbuddy', 'zcode'],
    round: 7, round_state: 'waiting',
    unread: 5, last_at: ago(18),
    topics: ROOM_TOPICS['g:round'].map(([tid, title], i) => ({
      id: tid, title, msg_count: ri(12, 96),
      private_count: i === 2 ? 2 : 0, last_at: ago(ri(18, 900)),
    })),
  });
  rooms.push({
    id: 'g:hall', type: 'group', title: '茶馆 · 闲话与梦境',
    members: ['ryuuzaki', ...AIDS],
    speak_mode: 'inviteonly',
    pending_speakers: ['dsh'],
    round: 3, round_state: 'running',
    unread: 0, last_at: ago(ri(60, 240)),
    topics: ROOM_TOPICS['g:hall'].map(([tid, title], i) => ({
      id: tid, title, msg_count: ri(4, 30),
      private_count: i === 0 ? 4 : 1, last_at: ago(ri(90, 2000)),
    })),
  });
  return rooms;
}

/* ================================================================
   帖流 — 每间房一串，含各种状态；私密帖 content=null
   ================================================================ */
let msgN = 0;
function mkMsg(room, over = {}) {
  const kind = over.kind ?? pick(['msg', 'chat', 'task', 'decision', 'system']);
  const f = FATE[kind];
  const other = room.type === 'dm' ? room.members.find((m) => m !== 'ryuuzaki') : pick(AIDS);
  const from = over.from ?? (rnd() < 0.45 ? 'ryuuzaki' : other);
  const to = over.to ?? (from === 'ryuuzaki'
    ? (f.audience === 'room' && room.type === 'group' ? AIDS : [other])
    : ['ryuuzaki']);
  const vis = over.visibility ?? (kind === 'decision' ? 'shared'
    : rnd() < 0.12 ? 'private' : rnd() < 0.1 ? 'shared_with' : 'shared');
  const sealed = vis === 'private';
  const state = over.state ?? (kind === 'task'
    ? pick(['acked', 'replied', 'delivered', 'timeout'])
    : pick(['replied', 'delivered', 'replied']));
  const mins = over.mins ?? ri(6, 2600);
  const m = {
    id: uid('m', ++msgN),
    room_id: room.id,
    topic_id: over.topic_id ?? (room.topics.length ? pick(room.topics).id : null),
    kind, state, from, to,
    content: sealed ? null : (over.content ?? pick(MSG_TEXT[kind])),
    visibility: vis,
    persist: f.persist, notify: f.notify, needs_ack: f.needs_ack, audience: f.audience,
    created_at: ago(mins),
    woke_at: f.notify && state !== 'timeout' ? ago(mins - 0.4) : null,
    reply_to: null,
    deadline_s: kind === 'task' ? 90 : 45,
    chunk_id: f.persist ? uid('c', ri(1, 3214)) : null,
  };
  if (sealed) m.redacted = true;
  if (vis === 'shared_with') m.shared_with = shuffle(AIDS).slice(0, ri(1, 2));
  if (state === 'streaming') m.stream = '正在核对第三节口径，已比出两处差值';
  return m;
}

function buildMessages(rooms) {
  const all = [];
  for (const r of rooms) {
    const n = r.type === 'group' ? ri(14, 22) : ri(10, 16);
    for (let i = 0; i < n; i++) all.push(mkMsg(r, { mins: ri(6, 3000) }));
  }
  /* 手工放几条"链上正在走"的帖，保证首屏一定有动态可看 */
  const xt = rooms.find((r) => r.id === 'dm:xuantie');
  const wb = rooms.find((r) => r.id === 'dm:workbuddy');
  const zc = rooms.find((r) => r.id === 'dm:zcode');
  all.push(mkMsg(xt, {
    kind: 'task', state: 'streaming', from: 'ryuuzaki', to: ['xuantie'], mins: 1.2,
    content: '把私有正文的三份无保护副本清一遍，先从向量缓存开始，做完给我条数',
    visibility: 'shared', topic_id: 't-xt-1',
  }));
  all.push(mkMsg(wb, {
    kind: 'task', state: 'waking', from: 'ryuuzaki', to: ['workbuddy'], mins: 0.4,
    content: '把今天淘汰的那批捞出来，分清哪些是被取代的、哪些是钟走完的',
    visibility: 'shared', topic_id: 't-wb-1',
  }));
  all.push(mkMsg(zc, {
    kind: 'task', state: 'timeout', from: 'ryuuzaki', to: ['zcode'], mins: 46,
    content: '把驾驶舱前端的接口对接说明补齐，逐端点列清楚',
    visibility: 'shared', topic_id: 't-zc-2',
  }));
  all.sort((a, b) => +new Date(b.created_at) - +new Date(a.created_at));
  return all;
}

/* ================================================================
   实体关系图 — 别名归一 + 共现 + 逆文档频率压枢纽
   idf 低 = 枢纽噪声（"记忆""XTMEM" 这种到处都连的词），UI 据此压暗
   ================================================================ */
const ENTITIES = [
  { label: '龙崎', kind: 'person', aliases: ['库主', 'Ryuuzaki', '龙总'], idf: 0.22 },
  { label: 'XTMEM', kind: 'project', aliases: ['共享记忆库', '公共库', '渊'], idf: 0.18 },
  { label: '可见性', kind: 'term', aliases: ['visibility', '权限三态'], idf: 0.62 },
  { label: '蒸馏', kind: 'term', aliases: ['distill', '上浮', 'promotion'], idf: 0.71 },
  { label: '衰减钟', kind: 'term', aliases: ['半衰期', 'decay', '代谢钟'], idf: 0.78 },
  { label: 'A2A', kind: 'term', aliases: ['Agent2Agent', 'v1.0 协议'], idf: 0.83 },
  { label: '唤醒桥', kind: 'term', aliases: ['WakeBridge', '拉起通道'], idf: 0.86 },
  { label: '演化边', kind: 'term', aliases: ['memory_evolution', '图边'], idf: 0.74 },
  { label: '向量缓存', kind: 'term', aliases: ['embedding_cache'], idf: 0.88 },
  { label: '玄铁', kind: 'agent', aliases: ['xuantie', 'hermes', '铁砧'], idf: 0.41 },
  { label: '小D', kind: 'agent', aliases: ['dsh', '鱼塘'], idf: 0.52 },
  { label: '江北', kind: 'agent', aliases: ['workbuddy', '茶馆'], idf: 0.55 },
  { label: 'Z指导', kind: 'agent', aliases: ['zcode', '雷总'], idf: 0.49 },
  { label: '差帖看板', kind: 'term', aliases: ['任务看板', 'task board'], idf: 0.91 },
  { label: 'E:\\2222\\3333\\new', kind: 'path', aliases: ['方案目录'], idf: 0.93 },
  { label: '每周梦境', kind: 'term', aliases: ['dream', '共同蒸馏'], idf: 0.95 },
  { label: '人情债', kind: 'term', aliases: ['favor debt'], idf: 0.96 },
  { label: '枢纽噪声', kind: 'term', aliases: ['hub noise', 'IDF 压制'], idf: 0.89 },
];

function buildGraph() {
  const entities = ENTITIES.map((e, i) => ({
    id: uid('e', i + 1), type: 'node_entity',
    label: e.label, aliases: e.aliases,
    mentions: Math.round((1 - e.idf) * 900) + ri(8, 60),
    idf: e.idf, kind: e.kind,
  }));

  /* 记忆节点：260 条窗口（全库 3214，见文件头 TODO）
     两层：约 1/4 浮在精炼层；寿命环按各自 owner 的钟走；少数已翻面（被取代） */
  const memories = [];
  for (let i = 0; i < 260; i++) {
    const owner = pick(AIDS);
    const clock = AGENTS.find((a) => a.id === owner).clock_rate;
    const refined = rnd() < 0.26;
    const vis = rnd() < 0.14 ? 'private' : rnd() < 0.08 ? 'shared_with' : 'shared';
    const sealed = vis === 'private';
    const retired = !refined && rnd() < 0.09;
    const ageMin = ri(20, 60 * 24 * 45);
    memories.push({
      id: uid('c', i + 1), type: 'node_memory',
      tier: refined ? 'refined' : 'raw',
      owner, visibility: vis,
      excerpt: sealed ? null : POOL[i % POOL.length],
      ...(sealed ? { redacted: true } : {}),
      importance: rf(0.2, 0.95),
      heat: rf(0, refined ? 1 : 0.7),
      life: retired ? 0 : +Math.max(0, Math.min(1, (refined ? 0.75 : 0.5) - (ageMin / (60 * 24 * 40)) * clock + rnd() * 0.3)).toFixed(3),
      ...(retired ? { retired: true } : {}),
      created_at: ago(ageMin),
    });
  }

  /* 边：记忆→实体（mentions）、实体↔实体（co，带 idf）、记忆→记忆（三种演化关系） */
  const edges = [];
  let en = 0;
  for (const m of memories) {
    const k = ri(1, 3);
    for (const e of shuffle(entities).slice(0, k)) {
      edges.push({ id: uid('g', ++en), source: m.id, target: e.id, rel: 'mentions', weight: rf(0.3, 1) });
    }
  }
  for (let i = 0; i < entities.length; i++) {
    for (let j = i + 1; j < entities.length; j++) {
      if (rnd() < 0.3) {
        const idf = +Math.min(entities[i].idf, entities[j].idf).toFixed(2);
        edges.push({ id: uid('g', ++en), source: entities[i].id, target: entities[j].id, rel: 'co', weight: rf(0.1, 1), idf });
      }
    }
  }
  const relOf = () => (rnd() < 0.5 ? 'supersedes' : rnd() < 0.6 ? 'related' : 'disagrees_with');
  const retiredOnes = memories.filter((m) => m.retired);
  for (const r of retiredOnes) {
    const newer = pick(memories.filter((m) => !m.retired && +new Date(m.created_at) > +new Date(r.created_at)));
    if (newer) edges.push({ id: uid('g', ++en), source: newer.id, target: r.id, rel: 'supersedes', weight: 1 });
  }
  for (let i = 0; i < 120; i++) {
    const a = pick(memories), b = pick(memories);
    if (a.id !== b.id) edges.push({ id: uid('g', ++en), source: a.id, target: b.id, rel: relOf(), weight: rf(0.2, 0.9) });
  }

  /* 六组两两关系：亲疏 / 信任 / 人情债 / 私信数 / 梦境
     数值参照公共库 mem-3dac504b 的实际反思记录，不是随手编的 */
  const BOND = {
    'zcode|workbuddy': { affinity: 0.8, trust: 0.75, favor_debt: -2, dm: 214, dreams: 6 },
    'zcode|xuantie': { affinity: 0.45, trust: 0.6, favor_debt: 1, dm: 96, dreams: 2 },
    'zcode|dsh': { affinity: 0.45, trust: 0.6, favor_debt: 0, dm: 74, dreams: 1 },
    'xuantie|dsh': { affinity: 0.58, trust: 0.71, favor_debt: 3, dm: 168, dreams: 4 },
    'xuantie|workbuddy': { affinity: 0.4, trust: 0.55, favor_debt: -1, dm: 52, dreams: 1 },
    'dsh|workbuddy': { affinity: 0.62, trust: 0.58, favor_debt: 2, dm: 121, dreams: 3 },
  };
  const bonds = Object.entries(BOND).map(([k, v]) => {
    const [a, b] = k.split('|');
    return {
      a, b, affinity: v.affinity, trust: v.trust, favor_debt: v.favor_debt,
      dm_count: v.dm, dreams: v.dreams, last_dm: ago(ri(30, 4000)),
    };
  });

  return { ok: true, entities, memories, edges, bonds };
}

/* ================================================================
   驾驶台状态 + 主动推上来的格子
   ⚠️ widget 私密的同样只给条数：body=null + redacted=true
   ================================================================ */
function buildWidgets() {
  return [
    {
      id: 'w-0001', from: 'xuantie', kind: 'metric',
      title: '向量缓存清理进度', body: '已清 2,914 / 3,148 行；剩下 234 行 chunk_id 为空，需要人判一次归属',
      visibility: 'shared', created_at: ago(26),
      actions: [{ id: 'a-keep', label: '按空值归档' }, { id: 'a-drop', label: '直接丢弃' }],
    },
    {
      id: 'w-0002', from: 'dsh', kind: 'ask',
      title: '想改一个默认值', body: '精炼层门禁的四条件里，第三条"跨域确认"我建议放宽成两票。要不要试一周？',
      visibility: 'shared', created_at: ago(88),
      actions: [{ id: 'a-yes', label: '试一周' }, { id: 'a-no', label: '维持四条件' }],
    },
    {
      id: 'w-0003', from: 'workbuddy', kind: 'dream',
      title: '本周梦境 · 与小D 共同蒸馏', body: null, redacted: true,
      visibility: 'private', created_at: ago(300),
    },
    {
      id: 'w-0004', from: 'zcode', kind: 'note',
      title: '留帖：会话已关', body: '我这盏是行灯，会话关了就只能留帖。有急事走玄铁转一手，她常明。',
      visibility: 'shared', created_at: ago(46),
    },
  ];
}

function buildState(rooms, msgs) {
  const agents = AGENTS.map((a) => {
    const lamp = a.id === 'xuantie' ? 'working'
      : a.id === 'dsh' ? 'online'
      : a.id === 'workbuddy' ? 'waking'
      : 'unreachable';
    const mem = a.id === 'xuantie' ? 1449 : a.id === 'dsh' ? 702 : a.id === 'workbuddy' ? 538 : 525;
    const priv = a.id === 'xuantie' ? 61 : a.id === 'dsh' ? 24 : a.id === 'workbuddy' ? 19 : 38;
    return {
      ...a, lamp, memories: mem, private_count: priv,
      unread: rooms.filter((r) => r.members.includes(a.id)).reduce((s, r) => s + (r.type === 'dm' ? r.unread : 0), 0),
      open_tasks: msgs.filter((m) => m.kind === 'task' && m.to.includes(a.id) && m.state !== 'acked').length,
      last_seen: lamp === 'unreachable' ? ago(46) : ago(rf(0.2, 6, 1)),
    };
  });
  const total = agents.reduce((s, a) => s + a.memories, 0);
  const refined = 812;
  return {
    ok: true,
    now: iso(NOW),
    liveness: 0.72,
    agents,
    vitals: {
      total, raw: total - refined, refined,
      domains: DOMAINS.length, clocks: CLOCKS.length,
      evo_edges: 837,
      evo_by_rel: { supersedes: 412, disagrees_with: 96, related: 329 },
      relations: 6,
      last_distill: ago(618),
      evicted_today: 23,
      refined_hit_rate: 0.64,
    },
    unread_total: rooms.reduce((s, r) => s + r.unread, 0),
    task_counts: {
      open: msgs.filter((m) => m.kind === 'task' && m.state !== 'acked').length,
      waiting_ack: msgs.filter((m) => m.kind === 'task' && (m.state === 'replied' || m.state === 'delivered')).length,
      done_today: 9,
    },
    recent: msgs.slice(0, 24),
    widgets: buildWidgets(),
  };
}

/* ================================================================
   写盘 — 正常盘 + 三种覆盖态（空 / 错 / 降级）
   ⚠️ 这三态的设计质量是隐藏考点，所以它们不是"随手写个空数组"：
   empty     全新的台：库里零条、四盏灯都没点、房间只有隐式对席房
   error     server_error：后端在但炸了，前端要能说清"炸在哪条腿"
   degraded  部分腿降级：state 能拿到但图谱拿不到，且带 stale_at
   ================================================================ */
const W = (name, data) => {
  writeFileSync(join(OUT, name), JSON.stringify(data, null, 2) + '\n', 'utf8');
  const kb = (JSON.stringify(data).length / 1024).toFixed(1);
  console.log(`[gen] ${name.padEnd(34)} ${kb.padStart(7)} KB`);
};

const rooms = buildRooms();
const msgs = buildMessages(rooms);
const state = buildState(rooms, msgs);
const graph = buildGraph();

W('cockpit-state.json', state);
W('rooms.json', { ok: true, rooms });
W('messages.json', { ok: true, messages: msgs });
W('graph-memory.json', graph);
W('clocks.json', { ok: true, clocks: CLOCKS });

/* ---------- 空态：全新的台 ---------- */
W('scenes/empty.json', {
  'cockpit-state': {
    ok: true, now: iso(NOW), liveness: 0,
    agents: AGENTS.map((a) => ({
      ...a, lamp: 'asleep', can_answer_now: false,
      unread: 0, open_tasks: 0, memories: 0, private_count: 0,
      last_seen: iso(NOW),
    })),
    vitals: {
      total: 0, raw: 0, refined: 0, domains: 0, clocks: 0, evo_edges: 0,
      evo_by_rel: { supersedes: 0, disagrees_with: 0, related: 0 },
      relations: 0, last_distill: iso(NOW), evicted_today: 0, refined_hit_rate: 0,
    },
    unread_total: 0,
    task_counts: { open: 0, waiting_ack: 0, done_today: 0 },
    recent: [], widgets: [],
  },
  rooms: {
    ok: true,
    rooms: AGENTS.map((a) => ({
      id: `dm:${a.id}`, type: 'dm', title: `与${a.name}`,
      members: ['ryuuzaki', a.id], speak_mode: 'sequential',
      pending_speakers: [], round: 0, round_state: 'idle',
      unread: 0, last_at: iso(NOW), topics: [],
    })),
  },
  graph: { ok: true, entities: [], memories: [], edges: [], bonds: [] },
});

/* ---------- 错误态：后端在但炸了 ---------- */
W('scenes/error.json', {
  'cockpit-state': {
    ok: false, code: 'server_error',
    message: '渊面读不出来：vector_store.db 被另一个进程独占（WAL 锁）',
    stale_at: ago(4),
  },
  rooms: { ok: false, code: 'server_error', message: 'rooms 表查询超时（>5s）', stale_at: ago(4) },
  graph: { ok: false, code: 'server_error', message: '图谱构建中断于 memory_evolution 扫描', stale_at: ago(11) },
});

/* ---------- 降级态：主状态在，图谱这条腿断了 ---------- */
W('scenes/degraded.json', {
  'cockpit-state': {
    ...state,
    liveness: 0.21,
    agents: state.agents.map((a) => ({
      ...a,
      lamp: a.id === 'xuantie' ? 'online' : 'unreachable',
      can_answer_now: a.id === 'xuantie',
    })),
    widgets: state.widgets.slice(0, 1),
    recent: state.recent.slice(0, 6),
  },
  rooms: { ok: true, rooms: rooms.map((r) => ({ ...r, pending_speakers: [], round_state: 'idle' })) },
  graph: {
    ok: false, code: 'unreachable', degraded: true,
    message: '图谱这条腿断了：只剩上次成功的快照可看',
    stale_at: ago(37),
  },
});

console.log(`[gen] seed=0x${SEED.toString(16)} now=${iso(NOW)}`);
console.log(`[gen] agents=${AGENTS.length} domains=${DOMAINS.length} clocks=${CLOCKS.length}`);
console.log(`[gen] rooms=${rooms.length} msgs=${msgs.length} entities=${graph.entities.length} memNodes=${graph.memories.length} edges=${graph.edges.length} bonds=${graph.bonds.length}`);
console.log(`[gen] private msgs=${msgs.filter((m) => m.visibility === 'private').length}（正文一律 null）`);
console.log('[gen] done.');
