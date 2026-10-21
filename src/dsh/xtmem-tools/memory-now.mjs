// XTMEM · 按当前问题召回（memory-now）纯函数
// 与宿主解耦：无 node 外部依赖，可 node:test。index.mjs 事件桥产出 data，本文件只做文本。

// ── 提示词插值防炸（2026-09-14 修「unknown prompt variable "{{agent}}"」）──────────────
// 现象：DSH 的 system-prompt 装配对每个 section/context 跑 interpolate()，凡出现 {{名字}}
//   就按模板变量解析；变量表只有 provider/model/cwd，命中未知名【直接抛异常】——整轮请求装配失败。
// 病灶来源：XTMEM 记忆是外部文本，可能【字面】写着双花括号（真实案例 mem-819c711b：
//   「重写 DEFAULT_INVITE_PROMPT（支持 {{agent}}/{{room}}）」），被 memory-now / 共享记忆段捞进提示词即炸。
// 修法：注入前统一清洗，把 {{ 的相邻两字符用零宽空格(U+200B)隔断——
//   ① 宿主扫描 text.indexOf('{{') 彻底扫不到，永不再抛；② 零宽空格不可见，人读渲染无感；
//   ③ 【不】改库、【不】动召回——脏文本只在落提示词这一刻被隔离，记忆资产保持原样。
// ⚠️ 新增任何"把 XTMEM 文本塞进 assembly"的注入点时，文本必须先过本函数。
const ZWSP = '\u200b';
// ⚠️ 2026-09-15 改：原实现 `split('{{').join('{'+ZWSP+'{')` 只处理**单个左游程对**，两个缺口：
//   ① 游程长度 ≥3 会**留尾巴**：'{{{'.split('{{') = ['','{'] → '{'+ZWSP+'{'+'{' → 末尾仍是连续两个左花括号，宿主照样匹配；
//   ② 原实现只管左花括号。
//   现改为按**连续同类游程**处理，保证输出中不存在任何连续同类花括号（幂等：输出无游程，再跑不变）。
// 判据与 Python 侧 memory/text_sanitize.py 的 neutralise_template_braces **同口径**（单一判据源，勿各写一遍）。
// ⚠️ 真源实测（dsh-system-prompt/lib/index.js:151-175）修正了原判据描述的第三行：
//   单独的**连续双右花括号【不】炸**（插值循环由 `{{` 驱动，无 `{{` 则根本不进循环），单独的 `{{`（后无 `}}`）也不炸。
//   真正炸的是：{{未注册名}} / {{{左游程}}} / {{名不合规}} / {{a}b}} 断裂组 —— 其中 3 类由 `{{` 触发。
//   本函数仍一并隔断双右：宁可隔断一千不漏一个，且左右游程在 {{{x}}} 这类形态里本就同时出现。
const BRACE_RUN = /\{{2,}|\}{2,}/g;
export function sanitizePromptText(text) {
  const s = String(text == null ? '' : text);
  return s.replace(BRACE_RUN, (run) => run.split('').join(ZWSP));
}

// ══ exposed 引用率记账（A16，2026-09-16）══════════════════════════════
// 背景：各端 exposed/cited/confirmed/corrected 全是 0，不是"没人看"，是"没记上"——
//   ① CLI 路径压根没有 exposed 记账；② 注入渲染不带 id，模型拿不到 id 就无从标注。
//   本区块解决第 ② 层：让注入行带上 id 标记，模型才能引用、扫描器才能认。
// ⚠️【单一事实源】：idTagOf 的形态、exposedIdsIn 的正则、与 provider.py 的 exposed 正则
//   三者必须同口径（都是 [m:mem-xxxxxxxx]）。改这里必须同步 provider.py，否则又造出
//   本单最开始的那类"格式不匹配"——第二代断链。
// ⚠️ [src:] 【不是】[m:]：[src:] 是血缘尾注（bundle 路径用），与 exposed 记账无关，勿混。
const ID_TAG_SHAPE = /^mem-[0-9a-f]{8}$/;

/** 造注入行尾部的 id 标记。形态不符就【不】加——加了扫描器扫不到 ⇒ 悄悄少记 = 假绿。 */
export function idTagOf(item) {
  const id = String((item && item.id) || '');
  return ID_TAG_SHAPE.test(id) ? ' [m:' + id + ']' : '';
}

/** 从最终注入文本里捞全部被标记的 chunk id（去重保序）。
 *  ⚠️ 正则每次新建：/g 正则带 lastIndex，复用同一实例会漏扫（经典 JS 坑）。 */
export function exposedIdsIn(text) {
  const out = [];
  const seen = new Set();
  const re = /\[m:(mem-[0-9a-f]{8})\]/g;
  let m;
  const s = String(text == null ? '' : text);
  while ((m = re.exec(s)) !== null) {
    if (!seen.has(m[1])) { seen.add(m[1]); out.push(m[1]); }
  }
  return out;
}


// ── 摘要批分桶（2026-09-16 单A7 · R2 剩余件①）─────────────────────────────────
// 病灶：memory-now 主窗（own 前 5 席）被「DSH 自动记忆」摘要批高浓度占位——实测近 30 天
//   107 个 own 窗口事件里 5/5 全被摘要吃满 10 件、≥3 席 22 件；raw-fallback（refined 层空、
//   回落 raw 流水）更重：22 件里 5/5 吃满 5 件。⇒ 真实高价值记忆挤不进这 5 席。
// 规则：按 content 前缀分桶，摘要批桶【最多占 2 / 5 席】，其余候选按原序顺延补位。
// ⚠️ 只限流、不过滤——被挤下的条目【不】删除、【不】回填，三条理由：
//   ① 它们仍在 chunks 里、仍能被 xtmem_recall 搜到（"记得住"不破）；
//   ② 这里只是"谁上这 5 席"的挑选，召回结果集合本身一字不动（"不过滤"）；
//   ③ 若【回填】被挤下的摘要来凑满 5 席，cap 就形同虚设（实测回填版席位分布 = 改前）。
// 代价（如实记账，别当成 bug）：候选池里非摘要料不足时本窗会少于 5 条——实测 refined-first
//   25.9%、raw-fallback 54.5% 的窗口会缩短；池内全是摘要的 19 件只剩 2 席。治本在取数侧
//   （memoryNowTop 5→8 之类）属 index.mjs，本单范围外，已写进回执建议。
// ⚠️【不】改前缀——本判据与写入端 index.mjs 的 `'DSH 自动记忆（turn '` 严格对齐（实测库内
//   937/937 全命中、0 例外）；若改写入端前缀【必须同步改这里】，否则分桶静默失效
//   （守卫 tests/memory-now.test.mjs 有一条"前缀对齐"断言盯着）。
// ── 单A17 追记（2026-09-16 · 聊天流水「只存不注入」）──────────────────────────
// A7 只做了限流（摘要批 ≤2/5 席），但流水本体仍占着窗口大头——实测 recall top5 里
//   流水常年 20~40%。A17 在限流【之前】加一道绝对排除：raw 层的 autoStore 流水【永不】
//   进注入窗。两层分工：A17 filter 打「raw流水」（库内 891 条，绝对排除）；A7 cap 管
//   「refined流水」（库内 58 条，相对限流 ≤2/5 席）——操作对象不同，不是同一件事管两遍。
// ⚠️ 这是「不注入」【不是】「删除」——chunks 里的流水一字不动（A12 定规：raw 层是
//   底层备份/血缘底座，永久保留，不是召回层）。本文件只管"谁上这 5 席"，不碰库。
const OWN_WINDOW = 5;
const SUMMARY_BUCKET_CAP = 2;
const SUMMARY_PREFIX = 'DSH 自动记忆（turn ';

/** 判是否摘要批桶：前缀严格匹配 autoStore 写入端（index.mjs 的 turn/end 拼接处）。
 *  为什么用前缀而不是结构化字段：recall 返回的 item 只有 id/content/score/namespace/
 *  project/visibility/tier/importance，【没有】track/derived_from/tags——结构判据取不到。 */
export function isSummaryBucket(item) {
  return String((item && item.content) || '').startsWith(SUMMARY_PREFIX);
}

/** 判是否「纯流水」（A17 判据，最窄口径）：tier==='raw' 且带流水前缀。
 *  语义 = autoStore 自动写、且从未被 promote 提升过的对话流水——正是"存着但不喂"的那批。
 *  为什么不是一刀切 tier=raw：会误伤 262 条非流水 raw（蒸馏产物/时间骨架）。
 *  为什么不是一刀切前缀：会误伤 58 条 refined 流水（已被提升 = 有人认定有价值，该留）。
 *  ⚠️ tier 缺失时 fail-open（不滤）——recall 实测恒带 tier，缺了是上游异常，
 *     宁可漏滤一条也不误杀（错题本 #60 否定式断言同款防御）。 */
export function isRawTurnLog(item) {
  return Boolean(item) && item.tier === 'raw' && isSummaryBucket(item);
}

/** pickOwnWindow()：契约
 *  入参：items（array，引擎 recall 返回的 own 候选，可能 >5 条——B2 锚点补召会加长）
 *  返回：{ picked: array, summarySeats: number }
 *  副作用：无（纯函数，不动入参）
 *  谁调用：memoryNowText()
 *  改动影响：OWN_WINDOW / SUMMARY_BUCKET_CAP 是注入面口径，改前先读上方分桶注释
 *  A17 后挑选顺序：先绝对排除纯流水（isRawTurnLog）→ 再对 refined流水 限流（cap）→ 上窗 */
export function pickOwnWindow(items) {
  const picked = [];
  let summarySeats = 0;
  for (const it of items) {
    if (picked.length >= OWN_WINDOW) break;
    if (isRawTurnLog(it)) continue;   // A17：纯流水永不进窗（不删除、不回填——它压根不该来）
    if (isSummaryBucket(it)) {
      if (summarySeats >= SUMMARY_BUCKET_CAP) continue;   // A7 限流：跳过（不删除、不回填）
      summarySeats += 1;
    }
    picked.push(it);
  }
  return { picked, summarySeats };
}

/** 按当前问题召回的渲染：入参 data（{at, query, own, cross}）+ label（本端域名，仅用于显示）。
 *  为什么 label 可传：多身份部署时不能把 "dsh" 写死在渲染里（显示错域比不显示更糟）。
 *  label 缺省 'dsh' 只为兼容旧调用；新调用一律显式传。 */
export function memoryNowText(data, label) {
  if (!data || !data.at || !data.query) return '';
  const tag = String(label || 'dsh');
  const own = (data.own && Array.isArray(data.own.items)) ? data.own.items : [];
  const cross = (data.cross && Array.isArray(data.cross.items)) ? data.cross.items : [];
  // A17：空判定挪到 pickOwnWindow 之后——own 候选全是纯流水时 picked 会空，
  // 若不在此拦截会渲染出"只有标题没有条目"的空段（标题行是 truthy，骗过旧的 !own.length 判定）。
  // 池空就空 = 宁少不假。
  const { picked } = pickOwnWindow(own);
  if (!picked.length && !cross.length) return '';
  const lines = ['【XTMEM 按当前问题召回（本轮相关记忆）】'];
  // 分桶在 own 主窗：raw-fallback 与 refined-first 走的是同一条渲染路径（本函数不看 mode），
  // 故两个分支天然都覆盖——不像取数侧那样需要分支判断。
  // A17 后 raw-fallback 分支实际只剩"非流水 raw + refined流水(cap)"，纯流水已在 pickOwnWindow 滤除。
  for (const it of picked) {
    const proj = it.project ? '/' + it.project : '';
    // A16：尾部带 id 标记（模型可引用 / 扫描器可记 exposed）。两条渲染路径【都要带】，
    // 只带一条 = 尺子只装半个刻度（另一条路径的记忆永久归因不上）。
    lines.push('- [' + tag + proj + '] ' + String(it.content || '') + idTagOf(it));
  }
  if (data.cross && cross.length) {
    lines.push('-- ' + tag + ' 域命中不足，已补充共享库：');
    for (const it of cross.slice(0, 3)) {
      const ns = it.namespace ? it.namespace : '?';
      // ⚠️ 标记加在 slice(0,200)【之后】——加在前面会被截断切掉，扫描器扫不到 = 漏记
      lines.push('- [共享库:' + ns + '] ' + String(it.content || '').slice(0, 200) + idTagOf(it));
    }
  }
  return lines.join('\n');
}

/** 给调用方的安全出口：内容可能来自 XTMEM（含字面 {{}} → 宿主插值会炸），
 *  故此处统一清洗后再交付。memoryNowText 本体保持纯拼接，便于单测。 */
export function memoryNowSectionText(data, label) {
  const t = memoryNowText(data, label);
  return t ? sanitizePromptText(t) : '';
}
