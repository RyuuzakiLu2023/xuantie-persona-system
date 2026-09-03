/* ================================================================
   照渊台 · 接口客户端（总导游图）★ mock 开关就在这里 ★

  ▍这是什么
  ① 干什么：八个端点的唯一调用出口；一个开关决定走假数据还是真后端
  ② 给谁看：后端上线后要接通的人（改一个环境变量即可）
  ③ 依赖：mock/engine.ts（假后端）+ api/endpoints.ts（类型）

  ▍怎么切到真后端（三种方式，优先级由高到低）
  1. 地址栏 ?mock=0            —— 临时，单次会话，演示时最方便
  2. .env.local 里 VITE_USE_MOCK=false —— 常态
  3. 默认 true（假数据）
  真后端根地址走 VITE_API_BASE，留空则同源 /api（vite.config.ts 里配了代理）。

  ▍结构
  flags()      读地址栏 + 环境变量，得出 { mock, scene, base, pollMs }
  api          八个方法，一一对应八个端点
  isFault()    ★ 所有调用方必须先过这个判断 ★ —— 错误态是一等公民，不是异常分支

  ▍修改铁律
  1. 组件里【不】许出现 fetch。所有网络出口只有这一个文件，
     否则接真后端那天要满仓库找 URL（血泪教训：这种散落一次就够受）。
  2. 真后端路径【不】许改写。契约定死八条，前端只做拼接不做重命名。
  3. 失败【不】抛异常。统一返回 ApiFault，让 UI 有机会画"降级态"而不是白屏。

  ▍更新记录
  *更新：2026-09-02 · 建档*
================================================================ */

import type { AgentId, MsgKind, Visibility, SpeakMode } from '../api/contract';
import type {
  ApiFault, CockpitState, RoomsRes, RoomContext,
  SendReq, SendRes, AckReq, AckRes, ScheduleReq, ScheduleRes,
  WidgetReq, WidgetRes, MemoryGraph, CreateRoomReq, CreateRoomRes,
} from '../api/endpoints';
import { MockBackend, type Scene, type Journey } from '../mock/engine';

/* ---------- 开关 ---------- */

export interface Flags {
  mock: boolean;
  scene: Scene;
  base: string;
  pollMs: number;
}

export function flags(): Flags {
  const q = new URLSearchParams(location.search);
  const envMock = (import.meta.env.VITE_USE_MOCK ?? 'true') !== 'false';
  const qm = q.get('mock');
  const scene = (q.get('scene') ?? 'normal') as Scene;
  const rawPoll = Number(import.meta.env.VITE_POLL_MS ?? 3000);
  return {
    mock: qm === null ? envMock : qm !== '0' && qm !== 'false',
    scene: (['normal', 'empty', 'error', 'degraded'] as Scene[]).includes(scene) ? scene : 'normal',
    base: (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, ''),
    /* ⚠️ 契约写死 2–5 秒。夹紧是防止有人把它调到 200ms 白烧 CPU（见 .env.example 铁律 2） */
    pollMs: Math.min(5000, Math.max(2000, Number.isFinite(rawPoll) ? rawPoll : 3000)),
  };
}

export const isFault = (x: unknown): x is ApiFault =>
  !!x && typeof x === 'object' && (x as { ok?: boolean }).ok === false;

/* ---------- 单例假后端 ---------- */

let _mb: MockBackend | null = null;
function mb(): MockBackend {
  const f = flags();
  if (!_mb) _mb = new MockBackend(f.scene);
  return _mb;
}

/** 演练：把命途时间往前拨。真后端下无效（真后端的时间不由前端说了算）。 */
export function skipAhead(ms: number) {
  if (flags().mock) mb().skip(ms);
}

/**
 * mockNow()：假后端的"现在"（含快进偏移）。
 * ⚠️ 命途进度必须按这个时刻算，【不】能直接用 Date.now()——
 *    按下快进后 mock 的时间被拨快了，用 Date.now() 会出现
 *    "假后端已经走完、时间轴还在第一幕"的对不上账（血泪：两套时钟必须同源）。
 *    真后端下退回 Date.now()：那时时间不由前端说了算。
 */
export function mockNow(): number {
  return flags().mock ? mb().now : Date.now();
}

/** 命途只在 mock 下存在——真后端靠轮询 state.recent 里的状态变化推断。 */
export function journeyOf(id: string): Journey | undefined {
  return flags().mock ? mb().journeyOf(id) : undefined;
}
export function allJourneys(): Journey[] {
  return flags().mock ? mb().allJourneys() : [];
}

/* ---------- 真后端的取用 ---------- */

async function http<T>(path: string, init?: RequestInit): Promise<T | ApiFault> {
  const f = flags();
  try {
    const res = await fetch(`${f.base}${path}`, {
      ...init,
      headers: { 'content-type': 'application/json', ...(init?.headers ?? {}) },
    });
    if (!res.ok) {
      return {
        ok: false,
        code: res.status >= 500 ? 'server_error' : 'forbidden',
        message: `${path} 返回 ${res.status}`,
      };
    }
    return (await res.json()) as T;
  } catch (e) {
    /* 后端还不存在时走这里：这不是 bug，是常态——UI 要画「渊面结冰」 */
    return {
      ok: false, code: 'unreachable',
      message: `连不上后端（${f.base || '同源'}${path}）：${(e as Error).message}`,
    };
  }
}

/* ================================================================
   八个端点
   ⚠️ 方法名与端点一一对应，别加"便捷方法"——加了就会有人绕过契约。
   ================================================================ */
export const api = {
  /** GET /api/cockpit/state */
  async state(): Promise<CockpitState | ApiFault> {
    if (flags().mock) return mb().getState() as CockpitState | ApiFault;
    return http<CockpitState>('/api/cockpit/state');
  },

  /** GET /api/rooms */
  async rooms(): Promise<RoomsRes | ApiFault> {
    if (flags().mock) return mb().getRooms() as RoomsRes | ApiFault;
    return http<RoomsRes>('/api/rooms');
  },

  /** POST /api/rooms —— 创建房间（对席/合席）★ 新增端点 ★ */
  async createRoom(req: CreateRoomReq): Promise<CreateRoomRes | ApiFault> {
    if (flags().mock) return mb().createRoom(req) as CreateRoomRes | ApiFault;
    return http<CreateRoomRes>('/api/rooms', { method: 'POST', body: JSON.stringify(req) });
  },

  /** GET /api/room/{id}/context */
  async roomContext(id: string, topicId: string | null, n = 40): Promise<RoomContext | ApiFault> {
    if (flags().mock) return mb().getRoomContext(id, topicId, n) as RoomContext | ApiFault;
    const q = new URLSearchParams({ n: String(n), ...(topicId ? { topic_id: topicId } : {}) });
    return http<RoomContext>(`/api/room/${encodeURIComponent(id)}/context?${q}`);
  },

  /** POST /api/msg/send —— 发消息（触发对端上工） */
  async send(req: SendReq): Promise<SendRes | ApiFault> {
    if (flags().mock) {
      return mb().send({
        room_id: req.room_id, topic_id: req.topic_id ?? null,
        to: req.to as AgentId[], kind: req.kind as MsgKind,
        content: req.content, visibility: req.visibility as Visibility,
        shared_with: req.shared_with, deadline_s: req.deadline_s,
      }) as SendRes | ApiFault;
    }
    return http<SendRes>('/api/msg/send', { method: 'POST', body: JSON.stringify(req) });
  },

  /** POST /api/task/ack —— 差帖回执 / 卡片按钮点击 */
  async ack(req: AckReq): Promise<AckRes | ApiFault> {
    if (flags().mock) return mb().ack(req) as AckRes | ApiFault;
    return http<AckRes>('/api/task/ack', { method: 'POST', body: JSON.stringify(req) });
  },

  /** POST /api/room/{id}/schedule —— 触发一轮群聊调度 */
  async schedule(id: string, req: ScheduleReq): Promise<ScheduleRes | ApiFault> {
    if (flags().mock) {
      return mb().schedule(id, {
        mode: req.mode as SpeakMode | undefined,
        invite: req.invite as AgentId[] | undefined,
      }) as ScheduleRes | ApiFault;
    }
    return http<ScheduleRes>(`/api/room/${encodeURIComponent(id)}/schedule`, {
      method: 'POST', body: JSON.stringify(req),
    });
  },

  /** POST /api/widget —— 某个 AI 主动推一格到首页 */
  async widget(req: WidgetReq): Promise<WidgetRes | ApiFault> {
    if (flags().mock) return mb().pushWidget(req) as WidgetRes | ApiFault;
    return http<WidgetRes>('/api/widget', { method: 'POST', body: JSON.stringify(req) });
  },

  /** GET /api/graph/memory */
  async graph(): Promise<MemoryGraph | ApiFault> {
    if (flags().mock) return mb().getGraph() as MemoryGraph | ApiFault;
    return http<MemoryGraph>('/api/graph/memory');
  },
};
