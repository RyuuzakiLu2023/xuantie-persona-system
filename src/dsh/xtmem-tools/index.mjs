// XTMEM · DSH 工具层插件（阶段 2.2）
// 挂载：xuantie profile 的 cordis.patch.yml → - insert: { id: xtmem-tools, name: './xtmem-tools/index.mjs' }
// 作用：为 profile 下**所有预设/会话**注册三工具（spawn xtmem.py，runBridge 模式）：
//   xtmem_recall / xtmem_store / xtmem_lookup_cross
// 零人格逻辑（D1/D5）：本文件不注入任何人设；记忆注入层另行（仅玄铁会话，后续阶段）。
// 更新：2026-08-25 · 阶段 2.2 初版（按 xuantie-webui 的 runBridge 模式）。
import { spawn } from 'node:child_process';
import { defineTool } from '@deepseek-ai/dsh-tools';

export const name = 'xtmem-tools';
export const inject = ['tools', 'systemPrompt'];

const DEFAULTS = {
  python: process.env.XTMEM_PYTHON || 'python',
  script: process.env.XTMEM_SCRIPT || 'xtmem/xtmem.py',
  timeoutMs: 30000,
  namespace: 'dsh',
};

/** spawn xtmem.py，收集 stdout，解析 JSON；失败/超时返回 { ok:false, error } */
function runXtmem(cfg, argv, { timeoutMs } = {}) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    try {
      const p = spawn(cfg.python, [cfg.script, ...argv], { windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' } });
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
  // 分角色注入：仅本角色 namespace（不 cross）；跨角色用工具显式查询
  const r = await runXtmem(cfg, ['lookup', '--top', '15', '--namespace', cfg.namespace]);
  if (r && r.ok && Array.isArray(r.items)) {
    _memCache = r;
    _memAt = Date.now();
  }
}
function memorySummaryText() {
  const items = (_memCache && _memCache.items) || [];
  if (!items.length) return '';
  const lines = ['【共享记忆（XTMEM 跨端可见）】'];
  for (const it of items.slice(0, 8)) {
    const ns = it.namespace ? it.namespace : '';
    const proj = it.project ? '/' + it.project : '';
    lines.push('- [' + ns + proj + '] ' + String(it.content || ''));
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
      if (!args.cross) argv.push('--as', String(args.namespace || cfg.namespace));
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
      const argv = ['lookup', '--cross', '--top', String(args.top ?? 50)];
      if (args.query) argv.splice(2, 0, '--query', String(args.query));
      return run(argv);
    },
  }));

  // 注入层：system-prompt/assemble 瀑布——只给玄铁 preset 会话追加共享记忆摘要（缓存 10 分钟）
  ctx.on('system-prompt/assemble', async (assembly, context, next) => {
    try {
      const preset = context?.agent?.session?.agentPreset;
      if (preset && preset !== 'xuantie') return next(assembly);
      if (!_memCache || Date.now() - _memAt > 600000) {
        try { await refreshMemoryCache(cfg); } catch { /* 保持旧缓存 */ }
      }
      const text = memorySummaryText();
      if (text) {
        assembly.sections = (assembly.sections || []).filter((s) => s.name !== 'xtmem:memory');
        assembly.sections.push({ name: 'xtmem:memory', order: 95, text });
        assembly.sections.sort((a, b) => a.order - b.order);
      }
    } catch { /* 注入失败不挡对话 */ }
    return next(assembly);
  });
}
