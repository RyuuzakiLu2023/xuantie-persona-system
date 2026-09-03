/* ================================================================
   照渊台 · 接口契约类型（总导游图）★ 本文件核心，改动前务必读懂 ★

  ▍这是什么
  ① 干什么：把已定死的 8 个端点翻译成 TypeScript 类型，前后端只认这一份
  ② 给谁看：写 mock 的人、接真后端的人、审字段语义的人
  ③ 依赖：无（纯类型，运行时零成本）

  ▍结构
  基础枚举        AgentId / Presence / LampState / MsgKind / MsgState / Visibility
  Message         五等帖的统一形状，含四个「命运位」
  Agent / Vitals  四端状态 + 库的体征
  八个端点        每个端点一对 Req/Res，命名 = 端点路径
  ★ 命运位 ★     persist / notify / needs_ack / audience —— 五等帖的差别在这四位，
                  不在 kind 这个名字上。UI 画的是这四位，不是标签颜色。

  ▍修改铁律
  1. 【不】擅改字段语义。契约是定死的，有疑问写进 docs/接口对接说明.md 的「疑问」节，
     不许在前端偷偷改名——改了名，后端上线那天就是一场对不上账的排雷。
  2. private 的正文在任何响应里都是 null。这【不】是"前端记得别显示"，
     是"后端根本不给"。见 Visibility 注释。
  3. 加字段用可选（`?`），删字段先在对接说明里挂废弃说明，别直接抹掉。

  ▍★ 前端重构 vs 接口重接的边界（库主 2026-09-02 确认）★
  前端要重构就重构 UI/交互/组件，只要【不】动本文件（与 endpoints.ts）里的
  字段名、字段语义、端点路径，后端接口【不】用重接。参考：把"四视图共渊"
  改成"主页会话 + 星渊图独立页"，本文件一个字符没改，接口零改动。
  只有改了契约（加参数/改字段名/合并拆分端点/加新端点）才需要重接，且必须
  先把疑问写进 docs/接口对接说明.md 等后端拍板（铁律 1 的延伸）。

  ▍外号表
  "帖"    → Message（五等：便帖/席上语/差帖/凭帖/风讯）
  "灯"    → Agent（四盏：常明/引火/檐下/行灯）
  "渊"    → 记忆库整体（raw 沉底 + refined 浮起两层）
  "封蜡"  → visibility=private 的呈现物：只给条数，不给正文
  "命运位" → persist / notify / needs_ack / audience 四个布尔/枚举

  ▍更新记录
  *更新：2026-09-02 · 建档，对齐八端点*
================================================================ */

/* ---------- 基础枚举 ---------- */

/** 四个主体。库主是 ryuuzaki，不是 agent，不占灯位。 */
export type AgentId = 'xuantie' | 'dsh' | 'workbuddy' | 'zcode';

/** 在线方式：他们不是四个头像，是四种"能不能立刻回话"的物理形态。 */
export type Presence =
  | 'resident'   /* 常驻：进程在跑，A2A 入站直达，可即时应答 */
  | 'on_demand'  /* 按需：要先把它拉起来，有冷启动代价 */
  | 'desktop'    /* 桌面端：人在机器前才醒，靠任务表驱动 */
  | 'session';   /* 命令行会话：会话开着才在，关了只能留帖 */

/** 灯的当下状态。UI 的灯焰运动逻辑直接由它驱动。 */
export type LampState =
  | 'online'      /* 焰稳，能立刻回话 */
  | 'waking'      /* 灯芯爆一下，正在被叫起来 */
  | 'working'     /* 快频摇曳，正在干活并流式回传 */
  | 'asleep'      /* 只剩余烬，要引火 */
  | 'unreachable';/* 焰灭，只能留帖 */

/**
 * 五等帖。差别不在名字，在四个命运位（见 Message.persist / notify / needs_ack / audience）：
 *   msg       便帖   不入渊、不打扰、无回执、对席可见 —— 看过即散
 *   chat      席上语 不入渊、不打扰、无回执、合席可见 —— 只在当下被看见
 *   task      差帖   入渊、打扰、要回执、对席可见 —— 会把人叫起来干活
 *   decision  凭帖   入渊、不打扰、无回执、合席可见 —— 悄悄归档，永久
 *   system    风讯   不入渊、不打扰、无回执、合席可见 —— 系统自己说的
 */
export type MsgKind = 'msg' | 'chat' | 'task' | 'decision' | 'system';

/** 一条帖从发出到回来（或超时）的状态机。这条链是驾驶舱最重要的动态。 */
export type MsgState =
  | 'queued'     /* 已投，还没碰到对端 */
  | 'waking'     /* 对端被唤醒中 */
  | 'streaming'  /* 中间过程正在流式回传 */
  | 'delivered'  /* 送达，对端已看见 */
  | 'replied'    /* 回复到达 */
  | 'acked'      /* 差帖已回执 */
  | 'timeout'    /* 叫不醒，降级为留帖：状态转黄，内容不丢 */
  | 'failed';    /* 通道本身出错 */

/**
 * 可见性三态。
 * ⚠️ private 是不可协商的产品纪律：后端【不】下发正文，content 恒为 null，
 *    只给 redacted=true 与条数。前端因此连"想显示"都没有数据可显示。
 */
export type Visibility = 'private' | 'shared' | 'shared_with';

/** 谁看得见：对席（两人）还是合席（整间房）。 */
export type Audience = 'pair' | 'room';

/** 记忆两层：沉底全量 / 浮起精炼。检索先问 refined，落空回落 raw。 */
export type Tier = 'raw' | 'refined';

/** 三种演化关系。代=同域新事取代旧事；争=跨域视角分歧（禁自动取代）；系=相系。 */
export type EvoRel = 'supersedes' | 'disagrees_with' | 'related';

/** 群聊三种发言调度。 */
export type SpeakMode = 'sequential' | 'naturerandom' | 'inviteonly';
