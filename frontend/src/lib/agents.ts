/* ================================================================
   照渊台 · 四盏灯的说法（总导游图）

  ▍这是什么
  ① 干什么：把契约里的枚举（presence / lamp）翻成人话，并按灯位固定顺序
  ② 给谁看：所有要显示"这盏灯是什么状态"的视图
  ③ 依赖：api/contract 的枚举类型

  ▍★ 为什么不放在 mock 里 ★
  mock/engine.ts 里有一张 NAMES 表，但那是假后端的东西——真后端上线后 mock 要删。
  界面拿名字必须从契约数据（Agent.name）里取，翻译表只翻枚举，不存事实。
  这样删掉 mock 那天，界面一个字都不用改。

  ▍修改铁律
  1. 【不】在这里写死四个人的名字/称号。名字来自 Agent.name，称号来自 Agent.epithet，
     都是后端下发的事实（血泪：写死一次，改名那天就有两处真相）。
  2. LAMP_ORDER 必须与 device/engine.ts 的 LAMP_ORDER 一致——灯位是空间记忆，
     顺序变了库主就要重新学一遍"谁在左边"。

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手）*
================================================================ */

import type { AgentId, LampState, Presence } from '../api/contract';
import type { Agent } from '../api/endpoints';

/** 灯位顺序。⚠️ 与 device/engine.ts 的 LAMP_ORDER 必须一致（见铁律 2）。 */
export const LAMP_ORDER: AgentId[] = ['xuantie', 'dsh', 'workbuddy', 'zcode'];

/** 在线方式的中文说法：说的是"能不能立刻回话"，不是"在不在线"。 */
export const PRESENCE_FACE: Record<Presence, { label: string; says: string }> = {
  resident: { label: '常明', says: '进程常驻，叫了就应。' },
  on_demand: { label: '引火', says: '要先拉起来，有冷启动的一段等待。' },
  desktop: { label: '檐下', says: '人在机器前才醒，靠任务表驱动。' },
  session: { label: '行灯', says: '会话开着才在，关了只能留帖。' },
};

/** 灯的当下状态。焰型的依据（见 device/engine.ts 的 drawLamps）。 */
export const LAMP_FACE: Record<LampState, { label: string; says: string }> = {
  online: { label: '焰稳', says: '常明着，能立刻回话。' },
  waking: { label: '爆芯', says: '正在被叫起来。' },
  working: { label: '在干活', says: '快频摇曳，边干边回传。' },
  asleep: { label: '剩烬', says: '睡着了，要引一次火。' },
  unreachable: { label: '焰灭', says: '够不上，只能留帖。' },
};

/** 按灯位排序，缺的灯不补空——契约给几盏就画几盏。 */
export function inLampOrder(list: Agent[]): Agent[] {
  return LAMP_ORDER.map((id) => list.find((a) => a.id === id)).filter((a): a is Agent => !!a);
}

/** 名字从契约取，取不到就退回 id（宁可露出 id，也【不】编一个名字）。 */
export function nameOf(list: Agent[], id: AgentId | 'ryuuzaki'): string {
  if (id === 'ryuuzaki') return '龙崎';
  return list.find((a) => a.id === id)?.name ?? id;
}
