/* ================================================================
   照渊台 · 台面 App（总导游图）★ 全站编排在这一处，改动前务必读懂 ★

  ▍这是什么
  ① 干什么：把三个视图、一个装置、八条轮询腿、键盘与鼠标动作编排成一台操作台
  ② 给谁看：加视图、加动作、改布局的人
  ③ 依赖：components/*（零件）、lib/hooks（轮询）、lib/journey（命途导演）、
     lib/keys（动作表）、lib/theme（材质）、lib/client（唯一网络出口）

  ▍★ 三个视图（库主拍板 2026-09-02：主页以会话窗口为主）★
  chat  会话（默认主页）  左：石舫与横档（房→话题）｜中：气泡往来 + 输入条｜右：四盏灯+推格
  task  差帖案            中：待回执差帖 + 今日了结
  star  星渊图            左：实体名单 ｜ 右：渊装置全屏 + 渊谱叠层（★ 星图撤到这里 ★）

  ▍★ 装置挂在哪（关键）★
  设备（AbyssStage / 渊）只在 star 视图挂载 —— 库主拍板『主页撤干净，星图独占』。
  【不】在 chat/task 视图渲染它。切到 chat 时装置卸载，切回 star 重建：
  命中测试 / 命途导演（useJourneyDirector）只在该装置存活时驱动。
  装置的纯视觉动画会在重建时重放一次——这是『独占页』的取舍，不破坏 mock 状态承载的因果。

  ▍★ 鼠标优先（库主拍板 2026-09-02 · Win 端）★
  每个动作都有一颗看得见的按钮：顶栏放常用的，『全部动作』面板（? 或点顶栏按钮）
  列全部。键位只是加速器。需要打字的只有两处：正文、检索词。

  ▍文件从上到下的结构
  layoutOf()    视图 → 列布局（★ 布局承载概念，只有这一处定义 ★）
  App           状态 → 数据腿 → 动作 → 键盘 → 渲染（顶栏 / 主区 / 体征条 / 浮层）
  三种态        空台 / 硬错（盖层）/ 降级（面板里的断腿卡，页面照旧可用）

  ▍状态清单（外号在括号里）
  view          当下视图
  focusAgent    (『只照这一盏』) 聚焦的灯 → 装置里其他灯与其记忆压暗
  focusMemoryId (『那一颗』) 选中的记忆 → 装置打金环，渊谱连线也跟着它
  focusEntity   渊谱里选中的实体
  trackedId     (『在追的那枚帖』) 命途时间轴画的是它
  castMsg       (『刚投出的那枚』) 投出瞬间的响应，补轮询还没追上的那一拍
  draft         递帖草稿 —— ★ 提在 App，所以切视图不丢草稿 ★
  probe         检索光柱的请求（q / 是否命中精炼层 / 时刻）

  ▍修改铁律
  1. 组件里【不】许 fetch。网络出口只有 lib/client.ts 的 api（见那份铁律 1）。
  2. 装置只挂 star 视图（见上文『装置挂在哪』）。【不】在 chat/task 里渲染它。
  3. 轮询失败【不】清空数据：旧数据 + 断腿卡才是降级态（见 hooks.ts 铁律 1）。
  4. 【不】新增端点。契约定死八条，疑问写进 docs/接口对接说明.md。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T1–T4）*
  *更新：2026-09-02 · 库主拍板重构：主页改为会话窗口，星图（渊+渊谱）撤成独立页*
================================================================ */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { AgentId } from './api/contract';
import type { Message } from './api/endpoints';
import { AbyssStage } from './components/AbyssStage';
import { FateTimeline } from './components/FateTimeline';
import { GraphOverlay } from './components/GraphOverlay';
import { ActionSheet, FaultCard, LampNiche, VitalsBar } from './components/Parts';
import { EntityRail, RecentRail, RoomRail, TaskBoard, WidgetGrid } from './components/Rails';
import { SendDesk, type Draft } from './components/SendDesk';
import { Conversation } from './components/Conversation';
import { ChatBar } from './components/ChatBar';
import type { AbyssEngine } from './device/engine';
import { hash32 } from './device/layout';
import { api, flags, skipAhead } from './lib/client';
import { clock, pct } from './lib/format';
import { useCockpit, useGraph, useNarrow, useNow, useRoomContext, useRooms } from './lib/hooks';
import { useJourneyDirector } from './lib/journey';
import { ACTIONS, VIEWS, matchKey, type KeyAct, type ViewId } from './lib/keys';
import { useTheme } from './lib/theme';
import { IconDayNight, IconLamp } from './ui/Glyphs';
/**
 * layoutOf()：视图 → 列布局。★ 布局承载概念，只有这一处定义 ★
 * 1  = 单列（手机上全都塌成这个）
 * 2l = 左栏 + 主区     2r = 主区 + 右栏     3 = 左 + 主区 + 右
 */
function layoutOf(view: ViewId): '1' | '2l' | '2r' | '3' {
  switch (view) {
    case 'chat': return '3';
    case 'task': return '1';
    case 'star': return '2l';
  }
}

const EMPTY_DRAFT: Draft = { to: null, kind: 'task', visibility: 'shared', sharedWith: [], text: '' };

export function App() {
  const f = useMemo(flags, []);
  const [theme, toggleTheme] = useTheme();
  const narrow = useNarrow(860);
  const now = useNow(1000);

  const [view, setView] = useState<ViewId>('chat');
  const [focusAgent, setFocusAgent] = useState<AgentId | null>(null);
  const [focusMemoryId, setFocusMemoryId] = useState<string | null>(null);
  const [focusEntity, setFocusEntity] = useState<string | null>(null);
  const [trackedId, setTrackedId] = useState<string | null>(null);
  const [castMsg, setCastMsg] = useState<Message | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [roomId, setRoomId] = useState<string | null>(null);
  const [topicId, setTopicId] = useState<string | null>(null);
  const [probe, setProbe] = useState<{ q: string; hitRefined: boolean; at: number } | null>(null);
  const [probeOpen, setProbeOpen] = useState(false);
  const [probeText, setProbeText] = useState('');
  const [helpOpen, setHelpOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [quickText, setQuickText] = useState('');

  const engineRef = useRef<AbyssEngine | null>(null);
  const textRef = useRef<HTMLTextAreaElement | null>(null);
  const probeRef = useRef<HTMLInputElement | null>(null);
  /* ---------- 四条数据腿（失败保留旧值，见 hooks 铁律 1） ---------- */
  const cockpit = useCockpit();
  const rooms = useRooms();
  const graph = useGraph();
  /* 会话上下文：主页必取（选中房/话题时），其它视图停轮询省电 */
  const ctx = useRoomContext(view === 'chat' ? roomId : null, topicId);

  const state = cockpit.data;
  const agents = state?.agents ?? [];
  const recent = state?.recent ?? [];
  const vitals = state?.vitals;

  /* 头一次拿到房间就选中第一间：库主进主页不该先面对一片空白 */
  useEffect(() => {
    if (!roomId && rooms.data?.rooms.length) setRoomId(rooms.data.rooms[0].id);
  }, [rooms.data, roomId]);

  /* ---------- 在追的那枚帖：轮询版优先，投出瞬间用 castMsg 补一拍 ---------- */
  const trackedMsg = useMemo<Message | null>(() => {
    if (!trackedId) return null;
    return recent.find((m) => m.id === trackedId)
      ?? (castMsg && castMsg.id === trackedId ? castMsg : null);
  }, [trackedId, recent, castMsg]);

  /* ---------- 命途导演：把幕翻成装置动作（唯一一处；装置在 star 视图挂载） ---------- */
  const jv = useJourneyDirector(engineRef, trackedMsg, view === 'star' && !!trackedMsg);
  /* ================================================================
     动作 —— 每一个都同时挂在按钮与键位上（见 lib/keys.ts ACTIONS）
     ================================================================ */

  /**
   * patchDraft()：契约
   * 入参：patch（Partial<Draft>，只写改动的字段）
   * 返回：无
   * 副作用：改 draft 状态
   * 谁调用：ChatBar / SendDesk 的每一处选择
   * 改动影响：⚠️ 必须是函数式合并（d => ...）。若改成 setDraft({...draft, ...patch})，
   *   同一批次里连点两下就会丢掉前一下的改动（见 错题本.md #4）。
   */
  const patchDraft = useCallback((patch: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...patch }));
  }, []);

  const goChat = useCallback(() => {
    setView('chat');
    requestAnimationFrame(() => textRef.current?.focus());
  }, []);

  const doCast = useCallback(async () => {
    if (!draft.to || !draft.text.trim() || !roomId) return;
    setBusy(true);
    const res = await api.send({
      room_id: roomId, topic_id: topicId, to: [draft.to],
      kind: draft.kind, content: draft.text.trim(),
      visibility: draft.visibility,
      ...(draft.visibility === 'shared_with' ? { shared_with: draft.sharedWith } : {}),
    });
    setBusy(false);
    if ('ok' in res && res.ok) {
      setCastMsg(res.message);
      setTrackedId(res.message.id);
      /* 只清正文：收件人/重量/可见性留着，连投几帖是常态（同上，函数式合并） */
      setDraft((d) => ({ ...d, text: '' }));
      setMoreOpen(false);
      cockpit.refresh();
    }
  }, [draft, roomId, topicId, cockpit]);

  const doAck = useCallback(async (m?: Message) => {
    const target = m ?? recent.find((x) => x.needs_ack && x.state !== 'acked');
    if (!target) return;
    setBusy(true);
    await api.ack({ message_id: target.id });
    setBusy(false);
    setTrackedId(target.id);
    cockpit.refresh();
  }, [recent, cockpit]);

  const doWidgetAction = useCallback(async (widgetId: string, actionId?: string) => {
    setBusy(true);
    const res = await api.ack({ message_id: widgetId, ...(actionId ? { action_id: actionId } : {}) });
    setBusy(false);
    if ('ok' in res && res.ok) setTrackedId(res.message.id);
    cockpit.refresh();
  }, [cockpit]);

  const doSchedule = useCallback(async (id: string) => {
    setBusy(true);
    await api.schedule(id, {});
    setBusy(false);
    rooms.refresh();
    cockpit.refresh();
  }, [rooms, cockpit]);

  /* 检索：打一道光柱下去。★ 命中精炼层与否由库的真实命中率决定 ★
     ⚠️ 用 hash32(q) 与 refined_hit_rate 比，【不】用 Math.random()：
        同一个词每次问都该得到同一个结果，否则演示时讲不清（见 engine 铁律 2）。 */
  const doProbe = useCallback((q: string) => {
    const rate = vitals?.refined_hit_rate ?? 0.5;
    setProbe({ q, hitRefined: hash32(q) < rate, at: Date.now() });
    setProbeOpen(false);
    setView('star');
  }, [vitals]);

  const openProbe = useCallback(() => {
    setProbeOpen(true);
    requestAnimationFrame(() => probeRef.current?.focus());
  }, []);

  /* 手机端「快回一句」：一句话 + 一颗按钮，默认递给第一盏能立刻回话的灯 */
  const doQuick = useCallback(async (kind: 'msg' | 'task') => {
    const to = agents.find((a) => a.can_answer_now)?.id ?? agents[0]?.id;
    if (!to || !quickText.trim() || !roomId) return;
    setBusy(true);
    const res = await api.send({
      room_id: roomId, topic_id: null, to: [to], kind,
      content: quickText.trim(), visibility: 'shared',
    });
    setBusy(false);
    if ('ok' in res && res.ok) {
      setCastMsg(res.message);
      setTrackedId(res.message.id);
      setQuickText('');
      cockpit.refresh();
    }
  }, [agents, quickText, roomId, cockpit]);
  /** 动作总线：按钮与键位都走这里（一处定义，见文件头鼠标优先纪律）。 */
  const dispatch = useCallback((a: KeyAct) => {
    switch (a.kind) {
      case 'view': setView(a.view); break;
      case 'send': goChat(); break;
      case 'ack': void doAck(); break;
      case 'probe': openProbe(); break;
      case 'theme': toggleTheme(); break;
      case 'help': setHelpOpen((v) => !v); break;
      case 'skip': skipAhead(8000); cockpit.refresh(); break;
      case 'escape':
        if (helpOpen) setHelpOpen(false);
        else if (probeOpen) setProbeOpen(false);
        else if (moreOpen) setMoreOpen(false);
        else if (trackedId) setTrackedId(null);
        else if (focusAgent) setFocusAgent(null);
        else if (focusMemoryId) setFocusMemoryId(null);
        break;
    }
  }, [goChat, doAck, openProbe, toggleTheme, cockpit, helpOpen, probeOpen, moreOpen, trackedId, focusAgent, focusMemoryId]);

  /* ---------- 键盘：加速器，不是唯一入口 ---------- */
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const act = matchKey(e);
      if (!act) return;
      e.preventDefault();
      dispatch(act);
    };
    window.addEventListener('keydown', on);
    return () => window.removeEventListener('keydown', on);
  }, [dispatch]);

  /* ================================================================
     三种态：空台 / 硬错 / 降级
     ⚠️ 降级【不】进盖层——旧数据还在，页面照旧可用，只在面板里挂断腿卡。
     ================================================================ */
  const hardFault = cockpit.fault && !cockpit.data ? cockpit.fault : null;
  const isEmpty = !!state && vitals?.total === 0 && recent.length === 0;

  /* 盖层 / 注解 / 渊谱叠层：只在星渊图（star）视图挂 —— 装置只在那一页存在 */
  const veil = view === 'star' ? (hardFault ? (
    <div className="abyss-veil">
      <div className="veil-box">
        <div className="veil-title">渊面读不出来</div>
        <div className="veil-text">
          {hardFault.message}
          {hardFault.stale_at && <><br />上次照见是 <span className="num">{clock(hardFault.stale_at)}</span>。</>}
          <br />页面没坏——是这条腿断了。它自己会重试（错一次退避一次）。
        </div>
        <div style={{ marginTop: 16 }}>
          <button className="btn" onClick={() => { cockpit.refresh(); rooms.refresh(); graph.refresh(); }}>
            立刻再试一次
          </button>
        </div>
      </div>
    </div>
  ) : isEmpty ? (
    <div className="abyss-veil">
      <div className="veil-box">
        <div className="veil-title">一口空渊</div>
        <div className="veil-text">
          水是干净的：还没有一条记忆沉底，四盏灯都只剩烬。
          <br />回主页去，投一枚帖下去，这池水就开始有东西了。
        </div>
        <div style={{ marginTop: 16 }}>
          <button className="btn" onClick={goChat}>回主页<span className="btn-key">1</span></button>
        </div>
      </div>
    </div>
  ) : null) : null;

  /* 渊面注解（只在星渊图） */
  const notes = view === 'star' ? (!hardFault && (probe || graph.fault || rooms.fault) ? (
    <>
      {probe && (
        <div className="probe-said" role="status">
          <b>照「{probe.q}」</b>
          <div>
            {probe.hitRefined
              ? '精炼层就照到了 —— 光柱到中层即止，这是快的那种。'
              : '精炼层落空，光柱穿过浑浊带探到了底 —— 这是慢的那种，慢在明面上。'}
          </div>
        </div>
      )}
      {graph.fault && (
        <div className="fault" role="status">
          <b>渊里的记忆点读不出来</b>
          <div>{graph.fault.message}</div>
          <div>
            水面、四盏灯与帖案照旧可用；缺的是沉浮的那些点
            {graph.fault.stale_at && <> · 上次照见 <span className="num">{clock(graph.fault.stale_at)}</span></>}
          </div>
        </div>
      )}
      {rooms.fault && !graph.fault && (
        <div className="fault" role="status">
          <b>房间册这条腿断了</b>
          <div>{rooms.fault.message}</div>
        </div>
      )}
    </>
  ) : null) : null;

  /* 渊谱叠层：在星渊图挂（挂了才起采样，见 AbyssStage 铁律 2） */
  const overlay = view === 'star' && graph.data
    ? (args: Parameters<NonNullable<Parameters<typeof AbyssStage>[0]['overlay']>>[0]) => (
      <GraphOverlay
        {...args}
        graph={graph.data!}
        focusEntity={focusEntity}
        onPickEntity={setFocusEntity}
        focusMemoryId={focusMemoryId}
      />
    )
    : undefined;

  const cols = narrow ? '1' : layoutOf(view);
  const room = rooms.data?.rooms.find((r) => r.id === roomId) ?? null;
  return (
    <div className="stage">
      {/* ================================================================
           顶栏 — 台名 / 活性灯 / 视图钮 / 动作钮 / 钟
         ================================================================ */}
      <header className="top">
        <div>
          <span className="brand">照 渊 台</span>
          {!narrow && <span className="brand-sub">一渊四灯 · XTMEM</span>}
        </div>

        <div className="liveness" title={`活性 ${pct(state?.liveness ?? 0)}：有帖在链上走就往上顶，安静时回落`}>
          <IconLamp size={13} style={{ color: 'var(--gold)' }} />
          <span className="liveness-bar" aria-hidden>
            <span className="liveness-fill" style={{ width: `${Math.round((state?.liveness ?? 0) * 100)}%` }} />
          </span>
          <span className="liveness-num num">{pct(state?.liveness ?? 0)}</span>
        </div>

        <nav className="btns views" aria-label="三个视图">
          {VIEWS.map((v, i) => (
            <button key={v.id} className="btn" aria-pressed={view === v.id}
              onClick={() => setView(v.id)} title={v.hint}>
              {v.label}<span className="btn-key">{i + 1}</span>
            </button>
          ))}
        </nav>

        <div className="btns" aria-label="常用动作">
          <button className="btn" onClick={goChat} title="起一份新帖（回主页）">
            递帖<span className="btn-key">n</span>
          </button>
          <button className="btn" onClick={openProbe} title="打一道检索光柱进渊里">
            检索<span className="btn-key">/</span>
          </button>
          {f.mock && (
            <button className="btn" onClick={() => { skipAhead(8000); cockpit.refresh(); }}
              title="把命途快进 8 秒（演练用，只在假后端下有效）">
              快进<span className="btn-key">s</span>
            </button>
          )}
          <button className="btn btn-icon" onClick={toggleTheme}
            title={theme === 'deep' ? '换成熟宣（白日）' : '换成夜渊（深色）'}
            aria-label="切换主题">
            <IconDayNight size={15} />
          </button>
          <button className="btn" onClick={() => setHelpOpen(true)} title="全部动作（也可按 ?）">
            全部动作<span className="btn-key">?</span>
          </button>
        </div>

        <div className="clock">
          {f.scene !== 'normal' && <span className="tag amber" style={{ marginRight: 8 }}>演练 {f.scene}</span>}
          {!f.mock && <span className="tag gold" style={{ marginRight: 8 }}>真后端</span>}
          {state ? clock(state.now) : '—:—:—'}
          {cockpit.fault && cockpit.data && <span className="tag amber" style={{ marginLeft: 8 }}>旧水</span>}
        </div>
      </header>
      {/* ================================================================
           主区 — 按视图出现左右栏与中列主体
         ================================================================ */}
      <div className="body" data-cols={cols}>
        {/* ---------- 左栏：会话的房册 / 星图的实体名单 ---------- */}
        {(view === 'chat' || view === 'star') && (
          <aside className="panel left">
            <div className="panel-head">
              <span className="panel-title">{view === 'chat' ? '石舫与横档' : '名单'}</span>
              <span className="panel-note">
                {view === 'chat' ? '房 → 话题' : `${graph.data?.entities.length ?? 0} 个实体`}
              </span>
            </div>
            <div className="panel-body">
              {view === 'chat' ? (
                <>
                  {rooms.fault && <FaultCard fault={rooms.fault} hasStale={!!rooms.data} what="房间册" />}
                  <RoomRail
                    rooms={rooms.data?.rooms ?? []} agents={agents} now={now}
                    roomId={roomId} topicId={topicId}
                    onPickRoom={setRoomId} onPickTopic={setTopicId} onSchedule={doSchedule}
                  />
                </>
              ) : (
                <>
                  {graph.fault && <FaultCard fault={graph.fault} hasStale={!!graph.data} what="图谱" />}
                  {graph.data
                    ? <EntityRail entities={graph.data.entities} focus={focusEntity} onPick={setFocusEntity} />
                    : !graph.fault && <div className="empty-note">正在捞图谱……</div>}
                </>
              )}
            </div>
          </aside>
        )}

        {/* ---------- 中列：会话气泡 / 差帖案 / 渊装置 ---------- */}
        <div className="center" data-center={view}>
          {view === 'chat' && (
            <>
              <Conversation
                ctx={ctx.data} agents={agents} now={now}
                trackedId={trackedId} onPick={(m) => setTrackedId(m.id)}
                fault={ctx.fault}
              />
              <ChatBar
                agents={agents} room={room} draft={draft}
                onPatch={patchDraft} onCast={() => void doCast()}
                onMore={() => setMoreOpen(true)}
                textRef={textRef} busy={busy}
              />
            </>
          )}

          {view === 'task' && (
            <>
              {state && <LampNiche agents={agents} focus={focusAgent} onFocus={setFocusAgent} />}
              <TaskBoard recent={recent} agents={agents} now={now}
                onAck={(m) => void doAck(m)} onPick={(m) => setTrackedId(m.id)}
                trackedId={trackedId} doneToday={state?.task_counts.done_today ?? 0} />
            </>
          )}

          {view === 'star' && (
            <AbyssStage
              agents={agents}
              memories={graph.data?.memories ?? []}
              bonds={graph.data?.bonds ?? []}
              vitals={vitals ?? { raw: 0, refined: 0, total: 0 }}
              liveness={state?.liveness ?? 0}
              probe={probe}
              focusMemoryId={focusMemoryId}
              focusAgent={focusAgent}
              engineRef={engineRef}
              onPickMemory={setFocusMemoryId}
              onPickLamp={(id) => setFocusAgent((v) => (v === id ? null : id))}
              overlay={overlay}
              veil={veil}
              notes={notes}
              theme={theme}
            />
          )}

          {/* 命途时间轴：在追一枚帖时浮在右下角（主页/星图都可见） */}
          {trackedMsg && jv.journey && (
            <FateTimeline
              view={jv} msg={trackedMsg} stream={trackedMsg.stream}
              canSkip={f.mock}
              onSkip={() => { skipAhead(8000); cockpit.refresh(); }}
              onClose={() => setTrackedId(null)}
            />
          )}
        </div>

        {/* ---------- 右栏：会话的灯龛+推格 ---------- */}
        {view === 'chat' && (
          <aside className="panel right">
            <div className="panel-head">
              <span className="panel-title">灯龛</span>
              <span className="panel-note">四盏灯的状态</span>
            </div>
            <div className="panel-body">
              {cockpit.fault && <FaultCard fault={cockpit.fault} hasStale={!!cockpit.data} what="驾驶舱" />}
              {state && <LampNiche agents={agents} focus={focusAgent} onFocus={setFocusAgent} />}
              {state?.widgets.length ? (
                <>
                  <div className="hairline" style={{ margin: '14px 0' }} />
                  <div className="group-title">灯推上来的</div>
                  <WidgetGrid widgets={state.widgets} agents={agents} now={now} onAction={doWidgetAction} />
                </>
              ) : null}
              <div className="hairline" style={{ margin: '14px 0' }} />
              <div className="group-title">最近往来</div>
              {state && <RecentRail recent={recent} agents={agents} now={now}
                onPick={(m) => setTrackedId(m.id)} trackedId={trackedId} />}
            </div>
          </aside>
        )}
      </div>
      {/* ================================================================
           体征条 — 底部一行小字（质感，不是卖点）
         ================================================================ */}
      {vitals
        ? <VitalsBar v={vitals} liveness={state?.liveness ?? 0} now={now} />
        : <div className="vitals"><span className="vital">渊面还没读到</span></div>}

      {/* ================================================================
           手机端「快回一句」— 主操作永远在手边（spec §T4）
           ⚠️ 桌面 display:none，所以这一行在桌面不占高度（见 app.css .quick）
         ================================================================ */}
      <div className="quick">
        <input className="text-input" value={quickText} placeholder="回一句…"
          onChange={(e) => setQuickText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && quickText.trim()) void doQuick('msg'); }} />
        <button className="btn" disabled={!quickText.trim() || busy} onClick={() => void doQuick('msg')}
          title="发一张便帖：不入渊、不打扰">便帖</button>
        <button className="btn" disabled={!quickText.trim() || busy} onClick={() => void doQuick('task')}
          title="发一张差帖：入渊、把人叫起来、要回执">差帖</button>
      </div>

      {/* ---------- 浮层：全部动作 / 检索 / 完整递帖台 ---------- */}
      {helpOpen && <ActionSheet onAct={dispatch} onClose={() => setHelpOpen(false)} />}

      {probeOpen && (
        <div className="scrim" role="dialog" aria-modal="true" aria-label="检索"
          onClick={(e) => { if (e.target === e.currentTarget) setProbeOpen(false); }}>
          <div className="sheet" style={{ maxHeight: 'none' }}>
            <div className="corner tl" /><div className="corner br" />
            <div className="sheet-head">
              <div>
                <div className="panel-title">照一照</div>
                <div className="panel-note">
                  光柱先扫精炼层；落空才穿过浑浊带探底——所以落空看起来更慢，那是真的更慢。
                </div>
              </div>
              <button className="btn" onClick={() => setProbeOpen(false)}>关闭<span className="btn-key">Esc</span></button>
            </div>
            <div className="sheet-body">
              <div className="field">
                <input ref={probeRef} className="text-input" value={probeText}
                  placeholder="查一个词，例如：可见性 / 幽灵 provider"
                  onChange={(e) => setProbeText(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && probeText.trim()) doProbe(probeText.trim()); }} />
                <div className="field-head">
                  <button className="btn-cast" disabled={!probeText.trim()}
                    onClick={() => doProbe(probeText.trim())}>打光柱</button>
                  <span className="field-hint">
                    当前命中精炼层的比率 <span className="num">{pct(vitals?.refined_hit_rate ?? 0, 1)}</span>
                  </span>
                </div>
              </div>
              {probe && (
                <div className="preview" style={{ marginTop: 12 }}>
                  <span className="lead">上一道</span>：「{probe.q}」
                  {probe.hitRefined ? '在精炼层就照到了——光柱到中层即止。' : '精炼层落空，光柱穿过浑浊带探到了底。'}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {moreOpen && room && (
        <div className="scrim" role="dialog" aria-modal="true" aria-label="完整递帖台"
          onClick={(e) => { if (e.target === e.currentTarget) setMoreOpen(false); }}>
          <div className="sheet">
            <div className="corner tl" /><div className="corner tr" />
            <div className="corner bl" /><div className="corner br" />
            <div className="sheet-head">
              <div>
                <div className="panel-title">完整递帖台</div>
                <div className="panel-note">定向勾人 · 预演命途（收件人/重量/可见性/正文）</div>
              </div>
              <button className="btn" onClick={() => setMoreOpen(false)}>关闭<span className="btn-key">Esc</span></button>
            </div>
            <div className="sheet-body">
              <SendDesk
                agents={agents}
                room={room}
                draft={draft} onPatch={patchDraft} onCast={() => void doCast()}
                textRef={textRef} busy={busy}
              />
            </div>
          </div>
        </div>
      )}

      {/* 读屏器用的动作清单：鼠标优先不等于放弃无障碍（见文件头） */}
      <div className="sr" aria-live="polite">
        {jv.current ? `命途：${jv.current.says}` : ''}
      </div>
      <div className="sr">
        可用动作：{ACTIONS.map((a) => `${a.says}（${a.keys || '仅按钮'}）`).join('；')}
      </div>
    </div>
  );
}