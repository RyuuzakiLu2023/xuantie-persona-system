/* ================================================================
   照渊台 · 渊谱叠层（总导游图）★ 确定性布局，禁力导向 ★

  ▍这是什么
  ① 干什么：在渊之上叠一层"谱"——实体（人/项目/术语…）按 idf 分环排布，
     记忆点用装置当下的真坐标连到实体，演化边用不同线型表达三种关系
  ② 给谁看：改渊谱布局、加边类型的人
  ③ 依赖：AbyssStage 给的 points/geom（装置真坐标）、api/endpoints 的 MemoryGraph

  ▍★ 为什么连到装置的真坐标 ★
  谱不是另画一张图，是给同一池水加一层注解。所以实体连线的终点必须是
  装置里那颗点此刻的位置（AbyssStage 的 nodePoints()）——水在动，线就跟着动。
  另画一份坐标就会变成"两张图叠在一起"，视图之间的因果断掉。

  ▍★ idf 压暗就是"逆文档频率压制枢纽噪声"的视觉呈现 ★
  idf 低 = 万物皆连的噪声枢纽（比如"龙崎"提到 731 次）。它们压暗、缩小、
  排在外环；idf 高的稀有实体亮、大、排在内环。⚠️ 这【不】是装饰：
  它就是那条算法在界面上的样子（见 spec §三 T2-4）。

  ▍文件从上到下的结构
  entityLayout()   确定性布局：按 idf 分环 + hash 定角（★ 见铁律 1 ★）
  EDGE_STYLE       三种演化关系的线型（代/争/系）
  GraphOverlay     SVG 叠层：实体点 + 选中实体的连线 + 图例

  ▍修改铁律
  1. 【不】用力导向、【不】用 Math.random()。位置必须是 id/idf 的确定性函数——
     每次打开位置都变会砸掉空间记忆（同 layout.ts 铁律 2）。
  2. 叠层根节点必须 pointer-events:none，只有实体点自己可点（见 app.css 注释），
     否则整块 SVG 吃掉画布 hover，装置就"死"了。
  3. 连线上限 240 条（MAX_LINES）。全画（706 条）会糊成一团，也会掉帧。

  ▍外号表
  "谱"      → 这一层 SVG
  "枢纽"    → idf 低的实体（万物皆连，压暗）
  "代/争/系" → supersedes / disagrees_with / related 三种演化关系

  ▍更新记录
  *更新：2026-09-02 · 建档（小D 接手 T2-4）*
================================================================ */

import { useMemo } from 'react';
import type { EntityNode, GraphEdge, MemoryGraph } from '../api/endpoints';
import type { OverlayArgs } from './AbyssStage';
import { hash32 } from '../device/layout';

/** 连线上限（见铁律 3）。 */
const MAX_LINES = 240;

/**
 * 三种演化关系的线型。★ 代与争的区别是本作的一个命题 ★
 * 代 supersedes      实线带箭：同域新事取代旧事（旧的翻面但没被删）
 * 争 disagrees_with  双短划：跨域视角分歧，【不】自动取代——两条并列都还在
 * 系 related         点线：只是相系
 */
const EDGE_STYLE: Record<string, { dash: string; width: number; label: string }> = {
  supersedes: { dash: '', width: 1.4, label: '代' },
  disagrees_with: { dash: '5 3', width: 1.2, label: '争' },
  related: { dash: '1 3', width: 1, label: '系' },
  mentions: { dash: '2 4', width: 0.8, label: '提' },
  co: { dash: '1 5', width: 0.6, label: '共' },
};

interface EntityPos { e: EntityNode; x: number; y: number; r: number; dim: number }

/**
 * entityLayout()：契约
 * 入参：entities / 画布宽高
 * 返回：EntityPos[]（确定性）
 * 副作用：无
 * 谁调用：GraphOverlay
 * 改动影响：⚠️ 位置是空间记忆的地基，改了库主要重新学一遍（见铁律 1）。
 */
function entityLayout(entities: EntityNode[], w: number, h: number): EntityPos[] {
  /* idf 高（稀有）在内环，低（枢纽噪声）在外环 —— 这就是压制的样子 */
  const sorted = entities.slice().sort((a, b) => b.idf - a.idf);
  const cx = w * 0.5;
  const cy = h * 0.42;
  const rx = w * 0.42;
  const ry = h * 0.3;
  return sorted.map((e, i) => {
    const ring = e.idf > 0.62 ? 0 : e.idf > 0.38 ? 1 : 2;
    const kr = [0.46, 0.74, 1][ring];
    /* 角度由 id 散列定：稳定不漂；同环内按序号轻微错开，免得重叠 */
    const ang = hash32(e.id) * Math.PI * 2 + i * 0.021;
    return {
      e,
      x: cx + Math.cos(ang) * rx * kr,
      y: cy + Math.sin(ang) * ry * kr,
      /* 大小看被提到多少次，但压到很小的范围——它是注解不是主角 */
      r: 2.4 + Math.min(4.6, Math.log10(1 + e.mentions) * 2),
      /* 压暗：idf 越低越暗（枢纽噪声退到背景里） */
      dim: 0.28 + Math.min(0.72, e.idf * 1.15),
    };
  });
}

interface Props extends OverlayArgs {
  graph: MemoryGraph;
  /** 选中的实体：选了才画它的连线（全画会糊成一团） */
  focusEntity: string | null;
  onPickEntity: (id: string | null) => void;
  focusMemoryId: string | null;
}

export function GraphOverlay({ points, geom, graph, focusEntity, onPickEntity, focusMemoryId }: Props) {
  const ents = useMemo(
    () => entityLayout(graph.entities, geom.w, geom.rimY + 8),
    [graph.entities, geom.w, geom.rimY],
  );

  /* 要画的线：选中实体的 mentions 边 + 全部演化边（代/争/系是本作命题，一直画） */
  const lines = useMemo(() => {
    const byId = new Map(ents.map((p) => [p.e.id, p]));
    const out: { key: string; x1: number; y1: number; x2: number; y2: number; rel: string; on: boolean }[] = [];
    const push = (edge: GraphEdge, on: boolean) => {
      const a = byId.get(edge.source) ?? null;
      const b = byId.get(edge.target) ?? null;
      const ma = points.get(edge.source);
      const mb = points.get(edge.target);
      const p1 = a ? { x: a.x, y: a.y } : ma ? { x: ma.x, y: ma.y } : null;
      const p2 = b ? { x: b.x, y: b.y } : mb ? { x: mb.x, y: mb.y } : null;
      if (!p1 || !p2) return;
      out.push({ key: edge.id, x1: p1.x, y1: p1.y, x2: p2.x, y2: p2.y, rel: edge.rel, on });
    };
    /* 演化边优先（它们数量少但信息量最大） */
    for (const e of graph.edges) {
      if (e.rel === 'supersedes' || e.rel === 'disagrees_with' || e.rel === 'related') {
        const on = !focusMemoryId || e.source === focusMemoryId || e.target === focusMemoryId;
        push(e, on);
      }
      if (out.length >= MAX_LINES) break;
    }
    if (focusEntity) {
      for (const e of graph.edges) {
        if (out.length >= MAX_LINES) break;
        if (e.rel !== 'mentions' && e.rel !== 'co') continue;
        if (e.source !== focusEntity && e.target !== focusEntity) continue;
        push(e, true);
      }
    }
    return out;
  }, [graph.edges, ents, points, focusEntity, focusMemoryId]);

  return (
    <svg className="graph-overlay" width="100%" height="100%" aria-label="渊谱：实体与演化关系">
      {/* ---------- 线：先画，压在点下面 ---------- */}
      <g>
        {lines.map((l) => {
          const st = EDGE_STYLE[l.rel] ?? EDGE_STYLE.related;
          const col = l.rel === 'supersedes' ? 'var(--cinnabar)'
            : l.rel === 'disagrees_with' ? 'var(--amber)'
              : l.rel === 'related' ? 'var(--abyss)' : 'var(--gold-dim)';
          return (
            <line key={l.key}
              x1={l.x1} y1={l.y1} x2={l.x2} y2={l.y2}
              stroke={col} strokeWidth={st.width}
              strokeDasharray={st.dash || undefined}
              opacity={l.on ? 0.62 : 0.12} />
          );
        })}
      </g>

      {/* ---------- 实体点：可点（.hit 才吃事件，见铁律 2） ---------- */}
      <g>
        {ents.map((p) => {
          const on = focusEntity === p.e.id;
          return (
            <g key={p.e.id} className="hit entity-dot"
              opacity={on ? 1 : p.dim}
              onClick={(e) => { e.stopPropagation(); onPickEntity(on ? null : p.e.id); }}>
              <title>
                {p.e.label}
                {p.e.aliases.length ? `（也叫：${p.e.aliases.join('、')}）` : ''}
                {` · 被提到 ${p.e.mentions} 次 · idf ${p.e.idf.toFixed(2)}`}
                {p.e.idf < 0.38 ? ' · 枢纽噪声，已压暗' : ''}
              </title>
              <circle cx={p.x} cy={p.y} r={p.r}
                fill={on ? 'var(--gold)' : 'var(--abyss)'}
                stroke={on ? 'var(--gold)' : 'var(--gold-dim)'} strokeWidth={on ? 1.4 : 0.7} />
              {/* 名字只给内环与选中的实体，否则字会糊满整块 */}
              {(on || p.e.idf > 0.62) && (
                <text x={p.x + p.r + 4} y={p.y + 3.5}
                  fontSize={on ? 11 : 9.5}
                  fill={on ? 'var(--gold)' : 'var(--ink-2)'}
                  style={{ fontFamily: 'var(--f-body)' }}>
                  {p.e.label}
                </text>
              )}
            </g>
          );
        })}
      </g>

      {/* ---------- 图例：三种线型 ---------- */}
      <g transform={`translate(${Math.max(8, geom.padX)}, ${geom.h - 16})`} opacity={0.85}>
        {(['supersedes', 'disagrees_with', 'related'] as const).map((rel, i) => {
          const st = EDGE_STYLE[rel];
          const col = rel === 'supersedes' ? 'var(--cinnabar)' : rel === 'disagrees_with' ? 'var(--amber)' : 'var(--abyss)';
          const x = i * 78;
          return (
            <g key={rel} transform={`translate(${x},0)`}>
              <line x1={0} y1={-3} x2={20} y2={-3} stroke={col} strokeWidth={st.width} strokeDasharray={st.dash || undefined} />
              <text x={25} y={0} fontSize={10} fill="var(--ink-2)" style={{ fontFamily: 'var(--f-body)' }}>
                {st.label}·{rel === 'supersedes' ? '取代' : rel === 'disagrees_with' ? '分歧' : '相系'}
              </text>
            </g>
          );
        })}
      </g>
    </svg>
  );
}
