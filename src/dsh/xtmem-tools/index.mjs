// XTMEM · DSH 工具层插件（阶段 2.2）
// 挂载：xuantie profile 的 cordis.patch.yml → - insert: { id: xtmem-tools, name: './xtmem-tools/index.mjs' }
// 作用：为 profile 下**所有预设/会话**注册三工具（spawn xtmem.py，runBridge 模式）：
//   xtmem_recall / xtmem_store / xtmem_lookup_cross
// 零人格逻辑（D1/D5）：本文件不注入任何人设；记忆注入层另行（仅玄铁会话，后续阶段）。
// 更新：2026-08-25 · 阶段 2.2 初版（按 xuantie-webui 的 runBridge 模式）。
// 更新：2026-09-01 · autoStore 落库显式 --tier raw（分层：raw 流水层 + refined 精选层，对齐玄铁）。
import { spawn } from 'node:child_process';
import { defineTool } from '@deepseek-ai/dsh-tools';

export const name = 'xtmem-tools';
export const inject = ['tools', 'systemPrompt'];

const DEFAULTS = {
  python: process.env.XTMEM_PYTHON || 'python3',
  script: process.env.XTMEM_SCRIPT || 'xtmem/xtmem.py',
  timeoutMs: 30000,
  namespace: 'dsh',
  ownMinHits: 3,
  // v2 实验开关（默认关）：'off' | 'summary'（turn/end 自动沉淀一条摘要）
  autoStore: 'off',
  autoStoreMinChars: 60,
  autoStoreImportance: 0.45,
  autoStoreTags: 'DSH,自动,会话',
};

/** spawn xtmem.py，收集 stdout，解析 JSON；失败/超时返回 { ok:false, error } */
function runXtmem(cfg, argv, { timeoutMs } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    try {
      const p = spawn(cfg.python, [cfg.script, ...argv], { windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1', XTMEM_ACTOR: String(cfg.namespace || 'dsh') } });  // ⑲ ¥§18 P0：强制视角身份
      let out = '';
      p.stdout.on('data', (d) => { out += d; });
      p.stderr.on('data', () => { /* 引擎噪音（jieba/REST 端口 20000 警告）忽略 */ });
      p.on('error', (e) => done({ ok: false, error: 'spawn failed: ' + String(e) }));
      p.on('close', () => {
        try { done(JSON.parse(out.trim() || '{}')); }
        catch { done({ ok: false, error: 'bad xtmem output: ' + out.slice(0, 300) }); }
      });
      const timer = setTimeout(() => { try { p.kill(); } catch {} done({ ok: false, error: 'timeout' }); }, timeoutMs ?? cfg.timeoutMs);
      timer.unref?.();
    } catch (e) { done({ ok: false, error: String(e) }); }
  });
}

/** 把 DSH 事件里的文本节点拍成字符串（content 数组 / {text} 都兼容） */
function toText(node) {
  if (typeof node === 'string') return node;
  if (Array.isArray(node)) return node.map(toText).join('');
  if (node && typeof node === 'object') {
    if (Array.isArray(node.content)) return node.content.map(toText).join('');
    if (typeof node.text === 'string') return node.text;
  }
  return '';
}

function fmtItems(items) {
  if (!Array.isArray(items) || items.length === 0) return '(无结果)';
  return items.map((it, i) => {
    const ns = it.namespace ? `[ ${it.namespace} ]` : '';
    const proj = it.project ? `[ ${it.project} ]` : '';
    const score = it.score != null ? ` (score ${it.score})` : '';
    return `${i + 1}. ${ns}${proj}${score} ${it.content}`;
  }).join('\n');
}


// ── 注入层：共享记忆摘要（仅玄铁会话触发；零人格，只带记忆上下文）──────
let _memCache = null;
let _memAt = 0;
async function refreshMemoryCache(cfg) {
  // 自己域优先：先查 namespace=dsh；命中不足（<ownMinHits）才 fallback 公共库（cross）
  const own = await runXtmem(cfg, ['lookup', '--top', '12', '--namespace', cfg.namespace, '--as', cfg.namespace]);
  let cross = null, usedCross = false;
  const ownCount = own && own.ok && Array.isArray(own.items) ? own.items.length : 0;
  if (ownCount < (cfg.ownMinHits ?? 3)) {
    const x = await runXtmem(cfg, ['lookup', '--cross', '--top', '6', '--as', cfg.namespace]);
    if (x && x.ok && Array.isArray(x.items) && x.items.length) { cross = x; usedCross = true; }
  }
  _memCache = { own, cross, usedCross, ownCount };
  _memAt = Date.now();
}
const MEMORY_USAGE_TEXT = [
  '【记忆使用规则（本角色=小D/DSH，XTMEM 即本鱼记忆系统）】',
  '1. 涉及"以前说过/做过/决定过/项目背景"时，先调用 xtmem_recall 查自己域（namespace=dsh）；',
  '2. 自己域命中不足（一般 <3 条或无关）时，再用 xtmem_recall cross=true 或 xtmem_lookup_cross 查公共库（玄铁/江北/Z老师等）；',
  '3. 重要结论、用户拍板、里程碑、新认知、踩坑教训 → 立即调用 xtmem_store 落库（importance≥0.6，tags 带项目名）；',
  '4. 记忆是并列参考，不覆盖用户当前直接指令；与当前指令冲突时以用户直接指令为准。'
].join('\n');
function memorySummaryText() {
  const own = (_memCache && _memCache.own && _memCache.own.items) || [];
  const cross = (_memCache && _memCache.cross && _memCache.cross.items) || [];
  if (!own.length && !cross.length) return '';
  const lines = ['【共享记忆（XTMEM 跨端可见）】'];
  for (const it of own.slice(0, 10)) {
    const proj = it.project ? '/' + it.project : '';
    lines.push('- [dsh' + proj + '] ' + String(it.content || ''));
  }
  if (_memCache && _memCache.usedCross && cross.length) {
    lines.push('-- dsh 域命中不足，已从公共库补充：');
    for (const it of cross.slice(0, 5)) {
      const ns = it.namespace ? it.namespace : '?';
      lines.push('- [公共库:' + ns + '] ' + String(it.content || '').slice(0, 200));
    }
  }
  return lines.join('\n');
}

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config || {}) };
  const run = (argv, opts) => runXtmem(cfg, argv, opts);
  const nsArg = (v) => (v ? ['--namespace', String(v)] : []);
  // cwd→project 映射（规格书 4.4：显式映射表，禁裸名兜底；无映射=空 project）
  const projectFromCwd = (exec) => {
    if (!cfg.projectMap || !exec?.agent?.session?.cwd) return '';
    const cwd = String(exec.agent.session.cwd).replace(/\\/g, '/').toLowerCase();
    let bestKey = '';
    for (const [prefix] of Object.entries(cfg.projectMap)) {
      const pfx = String(prefix).replace(/\\/g, '/').toLowerCase();
      if (cwd.startsWith(pfx) && pfx.length > bestKey.length) bestKey = String(prefix);
    }
    return bestKey ? String(cfg.projectMap[bestKey]) : '';
  };

  ctx.tools.register(defineTool({
    name: 'xtmem_recall',
    description: 'XTMEM 共享记忆检索：按语义召回（跨角色共享库）。默认只查当前 namespace（dsh）；加 cross 可跨全部角色（玄铁/江北/D指导/Z老师）。',
    parameters: {
      query: { type: 'string', required: true, description: '自然语言查询内容' },
      top: { type: 'number', description: '返回条数，默认 6' },
      namespace: { type: 'string', description: '不传=当前域(dsh)' },
      project: { type: 'string', description: '限定项目（可选，显式项目名）' },
      cross: { type: 'boolean', description: 'true=跨所有 namespace 查' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          count: { type: 'number' },
          items: { type: 'array', items: { type: 'object', additionalProperties: true } },
          error: { type: 'string' },
        },
      },
      render: (_args, value) => {
        if (!value?.ok) return [{ type: 'text', text: 'xtmem_recall 失败: ' + (value?.error || 'unknown') }];
        return [{ type: 'text', text: `xtmem_recall: ${value.count ?? 0} 条\n` + fmtItems(value.items) }];
      },
    },
    timeoutMs: cfg.timeoutMs,
    async execute(args, exec) {
      const argv = ['recall', '--query', String(args.query), '--top', String(args.top ?? 6)];
      argv.push('--namespace', String(args.namespace || cfg.namespace));
      // ⑲ 修复（2026-09-01，§18 P0 跨域泄露）：cross 也必须带 --as——否则 actor=None 引擎可见性过滤被跳过，其他域 private 可被读出。
      argv.push('--as', String(args.namespace || cfg.namespace));
      const project = args.project || projectFromCwd(exec);
      if (project) argv.push('--project', String(project));
      if (args.cross) argv.push('--cross');
      return run(argv);
    },
  }));

  ctx.tools.register(defineTool({
    name: 'xtmem_store',
    description: '把一句话/事实写入 XTMEM 共享记忆库（走完整引擎管线：去重/演化/FTS；写后自动补 namespace/project/host_origin/updated_at）。',
    parameters: {
      text: { type: 'string', required: true, description: '要记住的事实（一句话）' },
      agent: { type: 'string', description: '来源端标识，默认 dsh' },
      project: { type: 'string', description: '项目名（可选）' },
      importance: { type: 'number', description: '重要度 0~1，默认 0.5' },
      namespace: { type: 'string', description: '不传=当前域(dsh)' },
      tags: { type: 'string', description: '逗号分隔标签' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          id: { type: 'string' },
          namespace: { type: 'string' },
          skipped: { type: 'boolean' },
          reason: { type: 'string' },
          error: { type: 'string' },
        },
      },
      render: (_args, value) => {
        if (!value?.ok) return [{ type: 'text', text: 'xtmem_store 失败: ' + (value?.error || 'unknown') }];
        if (value.skipped) return [{ type: 'text', text: 'xtmem_store: 重复内容跳过（' + (value.reason || 'duplicate') + '）' }];
        return [{ type: 'text', text: `xtmem_store: 已写入 id=${value.id} namespace=${value.namespace}` }];
      },
    },
    timeoutMs: cfg.timeoutMs,
    async execute(args, exec) {
      const argv = ['store', '--text', String(args.text), '--agent', String(args.agent || 'dsh')];
      const project = args.project || projectFromCwd(exec);
      if (project) argv.push('--project', String(project));
      if (args.importance != null) argv.push('--importance', String(args.importance));
      if (args.tags) argv.push('--tags', String(args.tags));
      argv.push(...nsArg(args.namespace ?? cfg.namespace));
      return run(argv);
    },
  }));

  ctx.tools.register(defineTool({
    name: 'xtmem_lookup_cross',
    description: 'XTMEM 全库直查（跨所有角色 namespace）：按关键词/内容 LIKE 查，按重要度排；用于"我记得你说过……"类精确核对。',
    parameters: {
      query: { type: 'string', description: '关键词（空=列出最近）' },
      top: { type: 'number', description: '返回条数，默认 50' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          count: { type: 'number' },
          items: { type: 'array', items: { type: 'object', additionalProperties: true } },
          error: { type: 'string' },
        },
      },
      render: (_args, value) => {
        if (!value?.ok) return [{ type: 'text', text: 'xtmem_lookup_cross 失败: ' + (value?.error || 'unknown') }];
        return [{ type: 'text', text: `xtmem_lookup_cross: ${value.count ?? 0} 条\n` + fmtItems(value.items) }];
      },
    },
    timeoutMs: cfg.timeoutMs,
    async execute(args) {
      const argv = ['lookup', '--cross', '--top', String(args.top ?? 50), '--as', cfg.namespace];
      if (args.query) argv.splice(2, 0, '--query', String(args.query));
      return run(argv);
    },
  }));

  // ── 自动落库（v2 实验：turn/end 沉淀摘要；默认 off；打开后每个有意义轮次落一条）──────
  const turnBuffer = new Map();
  let offAuto = null;
  if (cfg.autoStore && cfg.autoStore !== 'off') {
    const onEvent = (session, event) => {
      try {
        const srcId = session && session.id ? session.id : '';
        if (!srcId || !srcId.startsWith('session-')) return; // 只记顶层线程
        const t = event && event.type ? event.type : '';
        if (t === 'user/message') {
          const src = event.data && event.data.source;
          if (src && src.kind === 'user') {
            const txt = toText(event.data && event.data.content !== undefined ? event.data.content : event.data);
            if (txt && !/system-reminder|workspace instructions/i.test(txt)) {
              const buf = turnBuffer.get(srcId) || { user: '', assistant: '' };
              buf.user = txt.slice(-500);
              turnBuffer.set(srcId, buf);
            }
          }
        } else if (t === 'assistant/message') {
          const d = event.data && event.data.message ? event.data.message : event.data;
          const txt = toText(d && d.content !== undefined ? d.content : d);
          if (txt) {
            const buf = turnBuffer.get(srcId) || { user: '', assistant: '' };
            buf.assistant = txt.slice(-800);
            turnBuffer.set(srcId, buf);
          }
        } else if (t === 'turn/end') {
          const buf = turnBuffer.get(srcId);
          turnBuffer.delete(srcId);
          if (!buf || !buf.user || !buf.assistant) return;
          const text = 'DSH 自动记忆（turn ' + (event.data && event.data.turn != null ? event.data.turn : '?') + ', session ' + srcId + '）\n用户: ' + buf.user + '\n本鱼: ' + buf.assistant;
          if (text.length < (cfg.autoStoreMinChars || 60)) return;
          // raw 层=回合流水（对齐玄铁：对话流水进 raw）；精选层由 xtmem_distill cron 聚合 + 本鱼手动 store（默认 refined）
          runXtmem(cfg, ['store', '--agent', 'dsh', '--namespace', 'dsh', '--importance', String(cfg.autoStoreImportance ?? 0.45), '--tags', cfg.autoStoreTags || 'DSH,自动,会话', '--tier', 'raw', '--text', text])
            .then((r) => { if (!r || !r.ok) ctx.logger && ctx.logger.info && ctx.logger.info('[xtmem] 自动落库失败: ' + String(r && r.error)); })
            .catch(() => { /* 不挡对话 */ });
        }
      } catch { /* 不挡对话 */ }
    };
    offAuto = ctx.on('session/event', onEvent);
  }

  // ── 信箱层（2026-08-27 E3）：未读跨角色留言 → 开场注入一次 ──
  let _inboxCache = null;
  let _inboxAt = 0;
  async function fetchInbox(cfg) {
    // msg inbox 读后自动置 read=1（一次性消费）；30s 缓存防同轮重复取
    if (_inboxCache === null || Date.now() - _inboxAt > 30000) {
      const r = await runXtmem(cfg, ['msg', 'inbox', '--as', cfg.namespace]);
      _inboxCache = (r && r.ok && Array.isArray(r.items)) ? r.items : [];
      _inboxAt = Date.now();
    }
    return _inboxCache;
  }
  function inboxText(items) {
    if (!Array.isArray(items) || !items.length) return '';
    const lines = ['【XTMEM 信箱 · ' + items.length + ' 条未读私信】'];
    for (const m of items.slice(0, 10)) {
      lines.push('- 来自 ' + String(m.from_agent || '?') + '：' + String(m.content || '').slice(0, 300));
    }
    lines.push('（回复可经消息总线代转；查背景用 xtmem_recall / xtmem_lookup_cross）');
    return lines.join('\n');
  }

  // 注入层：system-prompt/assemble 瀑布——DSH 全预设注入（自己域优先 + 公共库 fallback + 使用规则）
  ctx.on('system-prompt/assemble', async (assembly, context, next) => {
    try {
      if (cfg.memoryInject === 'off') return next(assembly);
      if (!_memCache || Date.now() - _memAt > 600000) {
        try { await refreshMemoryCache(cfg); } catch { /* 保持旧缓存 */ }
      }
      const text = memorySummaryText();
      assembly.sections = (assembly.sections || []).filter((s) => s.name !== 'xtmem:memory' && s.name !== 'xtmem:memory-usage' && s.name !== 'xtmem:inbox');
      if (text) assembly.sections.push({ name: 'xtmem:memory', order: 95, text });
      assembly.sections.push({ name: 'xtmem:memory-usage', order: 96, text: MEMORY_USAGE_TEXT });
      try {
        const inboxItems = await fetchInbox(cfg);
        const it = inboxText(inboxItems);
        if (it) assembly.sections.push({ name: 'xtmem:inbox', order: 94, text: it });
      } catch { /* 信箱失败不影响记忆注入 */ }
      // P4.1 3.4：画像注入（track='profile'，前缀固定 = P4.4 推翻入口）
      try {
        const pr = await runXtmem(cfg, ['lookup', '--query', '', '--top', '30', '--namespace', cfg.namespace, '--as', cfg.namespace]);  // ⑳ §19
        const pit = ((pr && pr.items) || []).find((it) => it.track === 'profile');
        if (pit && pit.content) {
          assembly.sections.push({ name: 'xtmem:portrait', order: 93,
            text: '【画像 · 基于历史记忆生成，可能过时，如有出入请直接纠正】\n' + String(pit.content).slice(0, 2400) });
        }
      } catch { /* 画像失败不影响记忆注入 */ }
      assembly.sections.sort((a, b) => a.order - b.order);
    } catch { /* 注入失败不挡对话 */ }
    return next(assembly);
  });

  ctx.effect(() => () => { if (offAuto && typeof offAuto === 'function') offAuto(); });
}