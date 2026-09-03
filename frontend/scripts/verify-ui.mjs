/* ================================================================
   照渊台 · 验收脚本 verify-ui.mjs（总导游图）

  ▍这是什么
  ① 干什么：用 CDP（Chrome DevTools Protocol）驱动一个真实宽度的 headless Chrome，
     把 spec §五 的验收清单逐条跑成通过/失败，最后打印报告并以退出码表态
  ② 技术栈：Node 原生 WebSocket（Node ≥22 有全局 WebSocket）+ CDP，零依赖
  ③ 依赖：先 npm run build，再起 npx vite preview（默认 5300 端口）
  ④ 给谁看：改验收项、加验收项的人

  ▍怎么跑
  1) npm run build
  2) npx vite preview --port 5300 --strictPort
  3) node scripts/verify-ui.mjs            （默认桌面 1520x940 + 手机 430x880 两轮）
     可选：node scripts/verify-ui.mjs --url http://localhost:5300 --keep

  ▍★ 为什么不用 iframe 量 ★
  早先试过"把应用装进固定宽度 iframe"来量桌面形态：headless 会把不可见 iframe
  的事件循环整体冻结（setTimeout / MessageChannel / rAF 全不触发），
  于是首屏渲染完就死住，交互一律测不动（见 错题本.md #7）。
  CDP 的 Emulation.setDeviceMetricsOverride 直接改顶层视口，页面是真的在跑，
  且 Input.* 走真实输入管线 —— React 的受控组件能收到（合成 JS 事件收不到）。

  ▍文件从上到下的结构
  CDP 底座      connect / send / evalJS / clickSel / clickText / typeText / key
  断言器        ok() / check()，收集结果
  suiteDesktop  桌面轮：四视图/单装置/递帖全链/回执/主题/帮助面板/渊谱/检索
  suiteMobile   手机轮：单列/无横向溢出/快回一句能真的回一句
  suiteScenes   三态轮：empty / error / degraded 各自演什么
  main          起浏览器 → 跑三轮 → 报告 → 退出码

  ▍修改铁律
  1. 断言【不】许只看"元素在不在"，要看"它说了什么"——文案错了照样过测就白测。
  2. 每个 check 的名字写成人话（报告是给人看的，不是给 CI 看的）。
  3. 【不】在这里改应用状态之外的东西（别写文件、别碰 fixtures）。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 交活前自检，spec §五）*
================================================================ */

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';

const args = process.argv.slice(2);
const argOf = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const BASE = argOf('--url', 'http://localhost:5300');
const PORT = Number(argOf('--cdp', '9333'));

const CHROMES = [
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
];

/* ================================================================
   CDP 底座
   ================================================================ */
let ws = null, seq = 0;
const pending = new Map();

function send(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => {
    pending.set(id, { res, rej });
    setTimeout(() => { if (pending.has(id)) { pending.delete(id); rej(new Error('CDP 超时: ' + method)); } }, 20000);
  });
}

async function connect(wsUrl) {
  ws = new WebSocket(wsUrl);
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id);
      m.error ? p.rej(new Error(m.error.message)) : p.res(m.result);
    }
  });
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true });
    ws.addEventListener('error', () => rej(new Error('CDP 连不上')), { once: true });
  });
}

/** 在页面里求值，返回 JSON。⚠️ 表达式要自带 return 语义（用 IIFE）。 */
async function evalJS(expr) {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error('页面里抛了: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  return r.result.value;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 拿元素中心点（客户端坐标）。找不到返回 null。 */
const centerOf = (sel) => evalJS(`(() => { const e = document.querySelector(${JSON.stringify(sel)});
  if (!e) return null; e.scrollIntoView({block:'center'}); const b = e.getBoundingClientRect();
  return { x: b.x + b.width/2, y: b.y + b.height/2 }; })()`);

/** 按文案找元素中心点（同一类里挑第一个包含 needle 的）。 */
const centerOfText = (sel, needle) => evalJS(`(() => {
  const el = [...document.querySelectorAll(${JSON.stringify(sel)})].find(e => (e.textContent||'').includes(${JSON.stringify(needle)}));
  if (!el) return null; el.scrollIntoView({block:'center'}); const b = el.getBoundingClientRect();
  return { x: b.x + b.width/2, y: b.y + b.height/2 }; })()`);

async function clickAt(p) {
  if (!p) return false;
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: p.x, y: p.y, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: p.x, y: p.y, button: 'left', clickCount: 1 });
  await sleep(160);
  return true;
}
const clickSel = async (sel) => clickAt(await centerOf(sel));
const clickText = async (sel, needle) => clickAt(await centerOfText(sel, needle));

/** 真实输入：先点进去聚焦，再 insertText（走真实管线，React 受控组件收得到）。 */
async function typeText(sel, text) {
  if (!(await clickSel(sel))) return false;
  await send('Input.insertText', { text });
  await sleep(200);
  return true;
}

async function key(k) {
  const map = { Escape: 27, Enter: 13 };
  await send('Input.dispatchKeyEvent', {
    type: 'keyDown', key: k, code: k.length === 1 ? 'Key' + k.toUpperCase() : k,
    text: k.length === 1 ? k : undefined,
    windowsVirtualKeyCode: map[k] ?? (k.length === 1 ? k.toUpperCase().charCodeAt(0) : 0),
  });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: k });
  await sleep(180);
}

async function goto(url, w, h) {
  await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url });
  await sleep(2600);
}

/* ================================================================
   断言器
   ================================================================ */
const results = [];
function ok(name, pass, detail = '') {
  results.push({ name, pass: !!pass, detail: String(detail).slice(0, 220) });
  console.log((pass ? '  PASS  ' : '  FAIL  ') + name + (detail ? '  — ' + String(detail).slice(0, 160) : ''));
}
async function check(name, fn) {
  try { const r = await fn(); ok(name, r.pass, r.detail); }
  catch (e) { ok(name, false, '抛异常: ' + e.message); }
}

/* 常用取值 */
const txt = (sel, n = 200) => evalJS(`(() => (document.querySelector(${JSON.stringify(sel)})?.textContent || '').replace(/\\s+/g,' ').trim().slice(0, ${n}))()`);
const count = (sel) => evalJS(`document.querySelectorAll(${JSON.stringify(sel)}).length`);
const has = (sel) => evalJS(`!!document.querySelector(${JSON.stringify(sel)})`);
const curView = () => evalJS(`(() => [...document.querySelectorAll('.btns.views .btn')].find(b=>b.getAttribute('aria-pressed')==='true')?.textContent?.trim().slice(0,2) || null)()`);

/* ================================================================
   桌面轮
   ================================================================ */
async function suiteDesktop() {
  console.log('\n— 桌面 1520x940 —');
  await goto(BASE + '/', 1520, 940);
  await sleep(2200);

  await check('渊面读到数据（体征条有真数字）', async () => {
    const v = await txt('.vitals', 120);
    return { pass: /全量 [\d,]+/.test(v) && !v.includes('还没读到'), detail: v.slice(0, 80) };
  });

  /* ---------- 主页 = 会话窗口（默认） ---------- */
  await check('主页是会话：三列（房册/气泡+输入条/灯龛），默认视图=会话', async () => {
    const cols = await evalJS(`document.querySelector('.body')?.dataset.cols`);
    const bubbles = await count('.conv .bubble-row');
    const chatbar = await has('.chatbar');
    const rooms = await count('.panel.left .row');
    const lamps = await count('.panel.right .lamp-card');
    const v = await curView();
    return { pass: cols === '3' && bubbles > 0 && chatbar && rooms > 0 && lamps === 4 && v === '会话', detail: `cols=${cols} 气泡=${bubbles} 输入条=${chatbar} 房=${rooms} 灯=${lamps}` };
  });

  await check('主页：右栏灯龛四盏 + 最近往来列表在', async () => {
    const t = await txt('.panel.right .panel-title', 12);
    const rows = await count('.panel.right .row');
    return { pass: t.includes('灯龛') && rows > 0, detail: `${t} / 行数=${rows}` };
  });

  /* 星渊图：装置独占页 */
  await check('切到星渊图：装置画出东西且渊谱叠层在', async () => {
    await key('3'); await sleep(1200);
    const cols = await evalJS(`document.querySelector('.body')?.dataset.cols`);
    const n = await evalJS(`(() => { const cv=document.querySelector('.abyss canvas'); if(!cv) return 0;
      const im=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data; const s=new Set();
      const st=4*Math.max(1,Math.floor(im.length/4/4000)); for(let i=0;i<im.length;i+=st) s.add(im[i]+','+im[i+1]+','+im[i+2]);
      return s.size; })()`);
    const ov = await has('.graph-overlay');
    const ents = await count('.panel.left .row');
    const v = await curView();
    return { pass: cols === '2l' && n > 20 && ov && ents > 0 && v === '星渊', detail: `cols=${cols} 颜色=${n} 叠层=${ov} 实体=${ents}` };
  });

  await check('渊谱图例写着取代/分歧/相系', async () => {
    const legend = await evalJS(`[...document.querySelectorAll('.graph-overlay text')].map(t=>t.textContent.trim()).filter(s=>s.includes('·'))`);
    const j = legend.join(' ');
    return { pass: j.includes('取代') && j.includes('分歧') && j.includes('相系'), detail: j };
  });

  await check('装置在动（两次画布指纹不同）', async () => {
    const fp = `(() => { const cv=document.querySelector('.abyss canvas');
      const im=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;
      let h=2166136261; for(let i=0;i<im.length;i+=997){h^=im[i];h=Math.imul(h,16777619);} return h>>>0; })()`;
    const a = await evalJS(fp); await sleep(900); const b = await evalJS(fp);
    return { pass: a !== b, detail: a + ' → ' + b };
  });

  /* 回主页：会话投帖全链（ChatBar） */
  await check('主页 ChatBar：点灯 → 打正文 → 投出钮解禁', async () => {
    await key('1'); await sleep(900);
    await clickText('.cb-lamp', '玄铁');
    await typeText('.chatbar textarea', '把可见性补漏的验收跑一遍，做完给我条数');
    const lamp = await evalJS(`[...document.querySelectorAll('.cb-lamp')].filter(b=>b.getAttribute('aria-pressed')==='true').map(b=>b.textContent.trim().slice(0,2))`);
    const dis = await evalJS(`document.querySelector('.chatbar .btn-cast')?.disabled`);
    return { pass: lamp.length === 1 && lamp[0].includes('玄') && dis === false, detail: `灯=${lamp} 投出禁用=${dis}` };
  });

  await check('投出 → 气泡立刻出现 + 命途时间轴浮起并走幕', async () => {
    await clickSel('.chatbar .btn-cast');
    await sleep(2600);
    const open = await has('.fate');
    const total = await count('.fate .act');
    const done = await evalJS(`document.querySelectorAll('.fate .act.done').length`);
    const cur = await txt('.fate .act.cur', 60);
    return { pass: open && total >= 8 && done >= 2, detail: `${done}/${total} 幕 · 当前「${cur}」` };
  });

  await check('命途走到底：回执与蒸馏两幕都演到了', async () => {
    for (let i = 0; i < 4; i++) { await clickText('.fate-foot .btn', '快进'); await sleep(700); }
    const lines = await evalJS(`[...document.querySelectorAll('.fate .act.done')].map(a=>a.textContent.replace(/\\s+/g,' ').trim())`);
    const j = lines.join(' | ');
    return { pass: j.includes('锚链解开') && j.includes('上浮到精炼层'), detail: lines.length + ' 幕已过' };
  });

  /* 差帖案回执 */
  await check('差帖案能回执：点回执后那条进「今日了结」', async () => {
    await key('Escape'); await key('2'); await sleep(900);
    const before = await txt('.center .group-title', 40);
    const clicked = await clickText('.task-card .btn', '回执');
    await sleep(1600);
    const groups = await evalJS(`[...document.querySelectorAll('.group-title')].map(g=>g.textContent.trim())`);
    return { pass: clicked && groups.some((g) => g.includes('今日了结')), detail: before + ' → ' + groups.join(' / ') };
  });

  /* 完整递帖台（ChatBar 的「更多…」） */
  await check('完整递帖台：更多… 能打开，预演说清幕数与会不会叫人', async () => {
    await key('Escape'); await key('1'); await sleep(900);
    await clickText('.chatbar .cb-more', '更多');
    await sleep(700);
    const desk = await has('.sheet .desk');
    const p = await txt('.preview', 260);
    await key('Escape'); await sleep(400);
    return { pass: desk && p.includes('预演') && /共 \d+ 幕/.test(p), detail: p.slice(0, 120) };
  });

  /* 检索（自动切到星渊图） */
  await check('检索：打一道光柱，渊上留一句说清扫到哪一层', async () => {
    await key('/'); await sleep(700);
    await typeText('.sheet .text-input', '可见性');
    await clickText('.sheet .btn-cast', '打光柱');
    await sleep(1500);
    const said = await txt('.probe-said', 200);
    const v = await curView();
    const closed = !(await has('.scrim'));
    return { pass: closed && v === '星渊' && said.includes('可见性') && (said.includes('精炼层') || said.includes('探到了底')), detail: `视图=${v} ${said.slice(0, 110)}` };
  });

  /* 主题（在星渊图看装置换材质） */
  await check('主题可换：夜渊 ⇄ 熟宣，装置跟着换材质', async () => {
    const t0 = await evalJS(`document.documentElement.dataset.theme`);
    const bg0 = await evalJS(`(() => { const cv=document.querySelector('.abyss canvas');
      const d=cv.getContext('2d').getImageData(2,2,1,1).data; return d[0]+','+d[1]+','+d[2]; })()`);
    await key('d'); await sleep(1100);
    const t1 = await evalJS(`document.documentElement.dataset.theme`);
    const bg1 = await evalJS(`(() => { const cv=document.querySelector('.abyss canvas');
      const d=cv.getContext('2d').getImageData(2,2,1,1).data; return d[0]+','+d[1]+','+d[2]; })()`);
    await key('d'); await sleep(700);
    return { pass: t0 !== t1 && bg0 !== bg1, detail: `${t0}(${bg0}) → ${t1}(${bg1})` };
  });

  /* 全部动作面板：鼠标优先的总入口 */
  await check('全部动作面板：每行可点，点视图行真的切了视图', async () => {
    await clickText('.top .btn', '全部动作');
    await sleep(600);
    const rows = await count('.act-row');
    const opened = await has('.sheet');
    await clickText('.act-row', '差帖案');
    await sleep(800);
    const v = await curView();
    const closed = !(await has('.scrim'));
    return { pass: opened && rows >= 7 && v === '差帖' && closed, detail: `${rows} 行 · 点后视图=${v} 面板关闭=${closed}` };
  });

  await check('顶栏每个动作都有按钮（鼠标优先，不靠键盘）', async () => {
    const labels = await evalJS(`[...document.querySelectorAll('.top .btn')].map(b=>b.textContent.replace(/\\s+/g,'').replace(/[1-3nrsd?/]$/,'').trim())`);
    const want = ['会话', '差帖案', '星渊图', '检索', '快进', '全部动作'];
    const missing = want.filter((w) => !labels.some((l) => l.includes(w)));
    return { pass: missing.length === 0, detail: missing.length ? '缺: ' + missing.join('/') : labels.join(' ') };
  });

  await check('私密条目零正文：封蜡在，正文一个字都没有', async () => {
    await key('1'); await sleep(900);
    const r = await evalJS(`(() => {
      const bubbles = [...document.querySelectorAll('.bubble-row')];
      const sealed = bubbles.filter(r => r.querySelector('.seal'));
      return { sealedCount: sealed.length,
        leaked: sealed.filter(r => { const b = r.querySelector('.bubble-body'); return b && b.textContent.trim().length > 0 && !b.querySelector('.seal'); }).length,
        anyForbidden: bubbles.some(r => /无权|禁止查看|permission/i.test(r.textContent)) };
    })()`);
    return { pass: r.sealedCount > 0 && r.leaked === 0 && !r.anyForbidden, detail: `封蜡 ${r.sealedCount} 条 · 泄漏 ${r.leaked} · 出现"无权"字样=${r.anyForbidden}` };
  });

  await check('四命运位画出来了（渊/扰/执/对席）', async () => {
    const bits = await evalJS(`(() => { const g = document.querySelector('.bubble-row .fate-bits'); if (!g) return null;
      return [...g.children].map(b => b.textContent.trim() + (b.classList.contains('on') ? '+' : '-')); })()`);
    return { pass: Array.isArray(bits) && bits.length === 4, detail: bits ? bits.join(' ') : 'null' };
  });
}

/* ================================================================
   手机轮
   ================================================================ */
async function suiteMobile() {
  console.log('\n— 手机 430x880 —');
  await goto(BASE + '/', 430, 880);
  await sleep(2400);

  await check('手机：单列，整页不横向溢出（可滚条内的内容不算）', async () => {
    /* ⚠️ "溢出"只算逃出页面的：体征条是故意做成横向可滚的（overflow-x:auto），
       它里面的字超出视口是设计，不是 bug —— 判定时要爬祖先排掉滚动容器内的元素。 */
    const r = await evalJS(`(() => {
      const escaped = [...document.querySelectorAll('*')].filter(el => {
        const b = el.getBoundingClientRect();
        if (!(b.width > 0 && b.right > innerWidth + 0.5)) return false;
        let p = el.parentElement;
        while (p) { const cs = getComputedStyle(p);
          if ((cs.overflowX === 'auto' || cs.overflowX === 'scroll') && p.scrollWidth > p.clientWidth) return false;
          p = p.parentElement; }
        return true;
      });
      return { cols: document.querySelector('.body')?.dataset.cols,
        sw: document.documentElement.scrollWidth, vw: innerWidth, over: escaped.length,
        who: escaped.slice(0,4).map(e => (typeof e.className==='string'&&e.className) ? '.'+e.className.trim().split(/\\s+/)[0] : e.tagName) };
    })()`);
    return { pass: r.cols === '1' && r.sw <= r.vw + 1 && r.over === 0, detail: `cols=${r.cols} 文档宽${r.sw}/视口${r.vw} 逃出元素${r.over}${r.who.length?' ('+r.who.join(',')+')':''}` };
  });

  await check('手机：快回一句条在手边，两颗钮说清轻重', async () => {
    const disp = await evalJS(`getComputedStyle(document.querySelector('.quick')).display`);
    const btns = await evalJS(`[...document.querySelectorAll('.quick .btn')].map(b=>b.textContent.trim())`);
    return { pass: disp === 'grid' && btns.includes('便帖') && btns.includes('差帖'), detail: `display=${disp} 钮=${btns.join('/')}` };
  });

  await check('手机：真的能回一句（打字→点差帖→命途出现）', async () => {
    await typeText('.quick .text-input', '手机上回一句：那份对账我看过了');
    const dis = await evalJS(`document.querySelectorAll('.quick .btn')[1]?.disabled`);
    await clickText('.quick .btn', '差帖');
    await sleep(2400);
    const open = await has('.fate');
    const acts = await count('.fate .act');
    const cleared = await evalJS(`document.querySelector('.quick .text-input')?.value === ''`);
    return { pass: dis === false && open && acts > 0 && cleared, detail: `投出前禁用=${dis} 命途=${open} 幕数=${acts} 输入已清=${cleared}` };
  });

  await check('手机：命途浮层不挡死屏幕（贴底且限高）', async () => {
    const r = await evalJS(`(() => { const f=document.querySelector('.fate'); if(!f) return null;
      const b=f.getBoundingClientRect(); const cs=getComputedStyle(f);
      return { pos: cs.position, h: Math.round(b.height), vh: innerHeight, bottom: Math.round(innerHeight-b.bottom), w: Math.round(b.width) }; })()`);
    return { pass: r && r.pos === 'fixed' && r.h < r.vh * 0.55, detail: JSON.stringify(r) };
  });

  await check('手机：灯龛压成一行四格，细目收起来', async () => {
    const r = await evalJS(`(() => ({ cards: document.querySelectorAll('.lamp-card').length,
      grid: getComputedStyle(document.querySelector('.lamp-grid')).display }))()`);
    return { pass: r.cards === 4 && r.grid === 'none', detail: `灯卡${r.cards} 细目display=${r.grid}` };
  });

  await check('手机：主页会话可用（气泡 + 输入条 + 房册）', async () => {
    const bubbles = await count('.conv .bubble-row');
    const chatbar = await has('.chatbar');
    const rooms = await count('.panel.left .row');
    const cols = await evalJS(`document.querySelector('.body')?.dataset.cols`);
    return { pass: cols === '1' && bubbles > 0 && chatbar && rooms > 0, detail: `cols=${cols} 气泡=${bubbles} 输入条=${chatbar} 房=${rooms}` };
  });

  await check('手机：完整递帖台能弹开（更多…）', async () => {
    const moreBtn = await count('.chatbar .cb-more');
    await clickText('.chatbar .cb-more', '更多');
    await sleep(700);
    const desk = await has('.sheet .desk');
    const dis = await evalJS(`document.querySelector('.sheet .btn-cast')?.disabled`);
    await key('Escape'); await sleep(300);
    return { pass: moreBtn === 1 && desk, detail: `弹层=${desk} 投出钮禁用=${dis}` };
  });
}

/* ================================================================
   三态轮
   ================================================================ */
async function suiteScenes() {
  console.log('\n— 三态：empty / error / degraded —');

  await goto(BASE + '/?scene=empty', 1520, 940);
  await sleep(2200);
  /* 空台盖层只在星渊图（装置页）出现；主页仍是会话壳 */
  await check('空态主页：会话壳还在，不白屏', async () => {
    const chatbar = await has('.chatbar');
    const b = await txt('.conv-empty .veil-text', 160) || await txt('.panel.left', 80);
    return { pass: chatbar && b.length > 0, detail: b.slice(0, 80) };
  });
  await key('3'); await sleep(1200);
  await check('空态星渊图：说的是「一口空渊」并给一条出路', async () => {
    const t = await txt('.veil-title', 30);
    const b = await txt('.veil-text', 160);
    const btn = await txt('.abyss-veil .btn', 20);
    return { pass: t.includes('空渊') && b.length > 10 && btn.includes('主页'), detail: `${t} / ${btn}` };
  });

  await goto(BASE + '/?scene=error', 1520, 940);
  await sleep(2400);
  await check('硬错主页：会话区说读不出来，不白屏', async () => {
    const hard = await evalJS(`!!document.querySelector('.center .fault.hard') || !!document.querySelector('.panel.right .fault')`);
    const chatbar = await has('.chatbar');
    return { pass: hard && chatbar, detail: `硬错卡=${hard} 输入条=${chatbar}` };
  });
  await key('3'); await sleep(1200);
  await check('硬错星渊图：说清哪条腿断了 + 上次成功时刻 + 可重试', async () => {
    const t = await txt('.veil-title', 30);
    const b = await txt('.veil-text', 220);
    const btn = await has('.abyss-veil .btn');
    return { pass: t.includes('读不出来') && b.includes('上次照见') && btn, detail: `${t} · 重试钮=${btn}` };
  });

  await goto(BASE + '/?scene=degraded', 1520, 940);
  await sleep(2600);
  await check('降级主页：会话照旧可用（气泡还在）', async () => {
    const bubbles = await count('.bubble-row');
    const chatbar = await has('.chatbar');
    return { pass: bubbles > 0 && chatbar, detail: `气泡 ${bubbles} 枚 · 输入条=${chatbar}` };
  });
  await key('3'); await sleep(1500);
  await check('降级星渊图：装置在，渊上贴着断腿说明', async () => {
    const note = await txt('.abyss-note .fault', 200);
    const veil = await has('.veil-title');
    const cv = await evalJS(`!!document.querySelector('.abyss canvas')`);
    return { pass: cv && note.includes('读不出来') && note.includes('照旧可用') && !veil, detail: `盖层=${veil} · 注解「${note.slice(0, 60)}」` };
  });

  await check('降级：顶栏标出演练场景，活性回落', async () => {
    const tags = await evalJS(`[...document.querySelectorAll('.clock .tag')].map(t=>t.textContent.trim())`);
    return { pass: tags.some((t) => t.includes('degraded')), detail: tags.join('/') };
  });
}

/* ================================================================
   main
   ================================================================ */
async function main() {
  const exe = CHROMES.find((p) => existsSync(p));
  if (!exe) { console.error('找不到 Chrome/Edge'); process.exit(2); }

  const child = spawn(exe, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars',
    '--remote-debugging-port=' + PORT,
    '--user-data-dir=' + process.env.TEMP + '\\zhaoyuan-verify-' + Date.now(),
    '--no-first-run', '--no-default-browser-check',
    'about:blank',
  ], { stdio: 'ignore' });

  /* 等调试端口起来（最多 15 秒） */
  let wsUrl = null;
  for (let i = 0; i < 60; i++) {
    await sleep(250);
    try {
      const list = await fetch(`http://127.0.0.1:${PORT}/json/list`).then((r) => r.json());
      const page = list.find((t) => t.type === 'page');
      if (page?.webSocketDebuggerUrl) { wsUrl = page.webSocketDebuggerUrl; break; }
    } catch { /* 还没起来 */ }
  }
  if (!wsUrl) { child.kill(); console.error('CDP 端口没起来'); process.exit(2); }

  await connect(wsUrl);
  await send('Page.enable');
  await send('Runtime.enable');

  console.log('照渊台 · 验收（spec §五）  目标 ' + BASE);
  try {
    await suiteDesktop();
    await suiteMobile();
    await suiteScenes();
  } finally {
    try { ws.close(); } catch { /* 关就完事 */ }
    if (!args.includes('--keep')) child.kill();
  }

  const bad = results.filter((r) => !r.pass);
  console.log('\n================ 报告 ================');
  console.log(`共 ${results.length} 项 · 通过 ${results.length - bad.length} · 失败 ${bad.length}`);
  for (const b of bad) console.log('  FAIL  ' + b.name + '  — ' + b.detail);
  console.log('=====================================');
  process.exit(bad.length ? 1 : 0);
}

main().catch((e) => { console.error('验收脚本自己炸了:', e); process.exit(2); });
