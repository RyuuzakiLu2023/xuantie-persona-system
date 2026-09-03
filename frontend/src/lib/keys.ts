/* ================================================================
   照渊台 · 动作表与键位（总导游图）★ 鼠标优先，改动前务必读懂 ★

  ▍这是什么
  ① 干什么：全站「可做的事」的唯一出处。每个动作都同时是
     ——一颗看得见的按钮（鼠标主路径）
     ——一个键位（键盘加速器）
     ——「? 全部动作」面板里的一行（点了就执行）
  ② 给谁看：加动作、加快捷键、改帮助面板的人
  ③ 依赖：无（纯数据 + 一个匹配函数）

  ▍★ 鼠标优先纪律（库主拍板 2026-09-02：Win 端，尽量全鼠标点击）★
  这台操作台在 Windows 上用，主路径是鼠标：
    1. 【不】允许"只能靠键盘触发"的功能。任何 KeyAct 必须在界面上有一颗
       看得见、点得到的按钮——键位只是它的加速器，不是它的唯一入口。
    2. 需要打字的地方只有两处：递帖的正文、检索的词。其余一律点得完
       （收件人点灯卡、五等帖点卡、可见性点段、回执点按钮、快进点按钮）。
    3. 键盘仍然全程可用且必须有焦点态（base.css 的双线金框）——
       那是无障碍底线，【不】因为"主要用鼠标"就删掉。

  ▍★ 为什么动作表与键位表合一 ★
  分成两张表必然漂：改了行为忘了改帮助面板，用户按 ? 看到的就是假话。
  所以这里一张表三用（按钮 / 键位 / 帮助行），一处改，三处同步。

  ▍结构
  ViewId/VIEWS  四个视图（顺序 = 1-4 键的顺序，也是顶栏按钮的顺序）
  KeyAct        有哪些动作
  ACTIONS       ★ 表：动作 + 键名 + 中文说明 + 分组 —— 按钮与帮助面板都读它
  matchKey()    把 KeyboardEvent 翻成 KeyAct（输入框里除 Esc 外一律不拦）

  ▍修改铁律
  1. 【不】在组件里直接判 e.key，也【不】在别处另写一份动作清单。
     加动作先加进 ACTIONS，否则帮助面板会说谎、按钮会漏。
  2. 输入框内【不】拦截单字符键——否则打字就变成了切视图（血泪：n 是"递帖"，
     可库主正在正文里打"n"）。只有 Escape 与带修饰键的组合允许穿透。
  3. 【不】占用浏览器保留键（Ctrl+W / Ctrl+T / F5 这类）。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T4 键盘）*
  *更新：2026-09-02 · 库主拍板改鼠标优先：键位降级为加速器，ACTIONS 表一张三用*
================================================================ */

/** 三个视图。★ 顺序即顶栏按钮顺序，也是 1-3 键的顺序 ★
 *  库主拍板（2026-09-02）：主页以会话窗口为主，星图（渊+渊谱）撤成独立页。 */
export type ViewId = 'chat' | 'task' | 'star';

export const VIEWS: { id: ViewId; label: string; hint: string }[] = [
  { id: 'chat', label: '会话', hint: '四灯往来 · 于此递帖' },
  { id: 'task', label: '差帖案', hint: '还没了结的活' },
  { id: 'star', label: '星渊图', hint: '一口渊与它的图谱' },
];

export type KeyAct =
  | { kind: 'view'; view: ViewId }
  | { kind: 'send' }
  | { kind: 'ack' }
  | { kind: 'probe' }
  | { kind: 'theme' }
  | { kind: 'help' }
  | { kind: 'skip' }
  | { kind: 'escape' };

export interface ActionRow {
  /** 点了执行什么 */
  act: KeyAct;
  /** 键盘加速器的展示名（空串 = 这个动作只有按钮，没有键位） */
  keys: string;
  /** 中文说明。帮助面板与按钮的 title 都用这句 */
  says: string;
  group: '视图' | '动作' | '演练';
}

/**
 * ACTIONS —— ★ 一张表三用：顶栏按钮 / 键盘绑定 / 「? 全部动作」面板 ★
 * 视图四行由 VIEWS 派生，避免两处真相（见铁律 1）。
 */
export const ACTIONS: ActionRow[] = [
  ...VIEWS.map((v, i) => ({
    act: { kind: 'view' as const, view: v.id },
    keys: String(i + 1),
    says: `${v.label}：${v.hint}`,
    group: '视图' as const,
  })),
  { act: { kind: 'send' }, keys: 'n', says: '起一份新帖（跳到会话主页，光标落进正文）', group: '动作' },
  { act: { kind: 'ack' }, keys: 'r', says: '回执最上面那张差帖（锚链解开）', group: '动作' },
  { act: { kind: 'probe' }, keys: '/', says: '打一道检索光柱进渊里', group: '动作' },
  { act: { kind: 'theme' }, keys: 'd', says: '换主题：夜渊 ⇄ 熟宣', group: '动作' },
  { act: { kind: 'help' }, keys: '?', says: '开/关这张全部动作表', group: '动作' },
  { act: { kind: 'skip' }, keys: 's', says: '把命途快进 8 秒（只在假后端下有效）', group: '演练' },
];

/**
 * matchKey()：契约
 * 入参：e（KeyboardEvent）
 * 返回：KeyAct | null（null = 不拦，交给浏览器）
 * 副作用：无
 * 谁调用：App 的全局 keydown
 * 改动影响：⚠️ 键位来自 ACTIONS 表的 keys 字段，改键名要两处一起改（铁律 1）。
 */
export function matchKey(e: KeyboardEvent): KeyAct | null {
  if (e.key === 'Escape') return { kind: 'escape' };
  /* 修饰键组合一律放过：那是浏览器与系统的地盘 */
  if (e.ctrlKey || e.metaKey || e.altKey) return null;

  /* 输入框里只认 Esc（见铁律 2）——正在打正文时按 n 不该跳视图 */
  const t = e.target as HTMLElement | null;
  const tag = t?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable) return null;

  const k = e.key === '？' ? '?' : e.key.toLowerCase();
  const row = ACTIONS.find((r) => r.keys && r.keys.toLowerCase() === k);
  return row ? row.act : null;
}