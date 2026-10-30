"use client";
import "./callouts.css";
import { useMemo, useRef } from "react";
import { clusterAnchors, dimensionGeometry, layoutCallouts } from "../../lib/geometry.mjs";
import type { Severity } from "../../lib/types";

export interface CalloutAnchor {
  id: string; n: number; sev: Severity; ax: number; ay: number;
  title?: string; value?: string; opacity?: number; priority?: number;
}
export interface DimLine { id: string; a: [number, number]; b: [number, number]; text: string; sev: Severity; opacity?: number }
export interface Rect { x: number; y: number; w: number; h: number }

const R = 12;

export function CalloutLayer({
  anchors, dims = [], width, height, obstacles = [], selectedId, onSelect, onCluster, clusterRadius = 20,
}: {
  anchors: CalloutAnchor[]; dims?: DimLine[]; width: number; height: number; obstacles?: Rect[];
  selectedId: string | null; onSelect: (id: string | null) => void; onCluster?: (ids: string[]) => void; clusterRadius?: number;
}) {
  const prev = useRef(new Map<string, { bx: number; by: number }>());

  const { balloons, placed, clusters } = useMemo(() => {
    const visible = anchors.filter((a) => (a.opacity ?? 1) > 0.02 && a.ax > -40 && a.ay > -40 && a.ax < width + 40 && a.ay < height + 40);
    const keep = new Set(selectedId ? [selectedId] : []);
    const cl = clusterAnchors(visible.map((a) => ({ ...a, priority: a.priority ?? 0 })), clusterRadius, keep);
    const items = cl.map((c) => ({
      id: c.count === 1 ? c.members[0].id : `cluster:${c.ids.join("+")}`,
      ax: c.ax, ay: c.ay, priority: c.count === 1 && c.members[0].id === selectedId ? 999 : c.priority,
    }));
    const placedMap = layoutCallouts(items, { width, height, obstacles, radius: R, prev: prev.current });
    prev.current = new Map([...placedMap].map(([k, v]) => [k, { bx: v.bx, by: v.by }]));
    return { balloons: cl, placed: placedMap, clusters: cl };
  }, [anchors, width, height, obstacles, selectedId, clusterRadius]);
  void clusters;

  return (
    <div className="cl-root" style={{ width, height }}>
      <svg className="cl-svg" width={width} height={height} aria-hidden={false}>
        {dims.map((d) => (<Dimension key={d.id} d={d} />))}
        {balloons.map((c) => {
          const single = c.count === 1;
          const m = c.members[0] as CalloutAnchor;
          const id = single ? m.id : `cluster:${c.ids.join("+")}`;
          const p = placed.get(id);
          if (!p) return null;
          const sel = single && selectedId === m.id;
          const dimmed = selectedId && !sel;
          const sev = single ? m.sev : (c.members as CalloutAnchor[]).reduce((s: Severity, x) => rank(x.sev) < rank(s) ? x.sev : s, "info" as Severity);
          const col = `var(--sev-${sev})`;
          const op = (single ? (m.opacity ?? 1) : 1) * (dimmed ? 0.42 : 1);
          return (
            <g key={id} className={`cl-item ${sel ? "sel" : ""}`} style={{ opacity: op }}>
              <circle cx={c.ax} cy={c.ay} r={sel ? 3.4 : 2.4} fill={col} stroke="var(--stage)" strokeWidth="1" />
              {sel && <circle cx={c.ax} cy={c.ay} r="8" fill="none" stroke={col} strokeWidth="1" className="cl-ping" />}
              <path
                className="cl-leader" pathLength={1} fill="none" stroke={col} strokeWidth={sel ? 1.6 : 1.1}
                d={`M${c.ax} ${c.ay}L${p.ex} ${p.ey}L${p.sx} ${p.sy}`}
              />
              <g transform={`translate(${p.bx} ${p.by})`}>
              <g
                className="cl-balloon"
                onClick={(e) => { e.stopPropagation(); if (single) onSelect(sel ? null : m.id); else onCluster?.(c.ids); }}
                role="button" tabIndex={0} aria-label={single ? `${m.title ?? "Finding"} ${m.n}` : `${c.count} findings`}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); if (single) onSelect(sel ? null : m.id); else onCluster?.(c.ids); } }}
              >
                <circle r={R} className="cl-hit" fill="transparent" />
                <circle r={R} fill={sev === "critical" ? col : "var(--panel-solid)"} stroke={col} strokeWidth={sel ? 2 : 1.3} />
                {!single && <circle r={R + 3.5} fill="none" stroke={col} strokeWidth="0.8" strokeDasharray="2 2" />}
                <text textAnchor="middle" dy="0.34em" className="cl-n" fill={sev === "critical" ? "var(--on-signal)" : "var(--paper)"}>
                  {single ? m.n : c.count}
                </text>
              </g>
              </g>
            </g>
          );
        })}
      </svg>
      {selectedId && (() => {
        const a = anchors.find((x) => x.id === selectedId);
        const p = a && placed.get(a.id);
        if (!a || !p) return null;
        const w = 214;
        const right = p.bx + R + 10 + w < width - 6;
        const left = right ? p.bx + R + 10 : p.bx - R - 10 - w;
        const top = Math.min(Math.max(8, p.by - 22), height - 70);
        return (
          <div key={a.id} className="cl-tag hair panel" style={{ left, top, width: w }}>
            <div className="idx" style={{ color: `var(--sev-${a.sev})` }}>F-{String(a.n).padStart(3, "0")}</div>
            <div className="cl-tag-title">{a.title}</div>
            {a.value && <div className="cl-tag-val num">{a.value}</div>}
          </div>
        );
      })()}
    </div>
  );
}

const order: Severity[] = ["critical", "high", "medium", "low", "info"];
const rank = (s: Severity) => order.indexOf(s);

function Dimension({ d }: { d: DimLine }) {
  const g = dimensionGeometry(d.a, d.b, 18);
  const col = `var(--sev-${d.sev})`;
  const tw = d.text.length * 6.6 + 12;
  return (
    <g className="cl-dim" style={{ opacity: d.opacity ?? 1 }} stroke={col} fill="none" strokeWidth="1">
      {g.ext.map((e, i) => (<line key={i} x1={e[0][0]} y1={e[0][1]} x2={e[1][0]} y2={e[1][1]} strokeDasharray="0" />))}
      <line x1={g.line[0][0]} y1={g.line[0][1]} x2={g.line[1][0]} y2={g.line[1][1]} className="cl-dimline" pathLength={1} />
      {g.arrows.map((a, i) => (<polygon key={i} points={a.map((p) => p.join(",")).join(" ")} fill={col} stroke="none" />))}
      <rect x={g.mid[0] - tw / 2} y={g.mid[1] - 8} width={tw} height="16" fill="var(--panel-solid)" stroke={col} strokeWidth="0.8" />
      <text x={g.mid[0]} y={g.mid[1]} textAnchor="middle" dy="0.34em" className="cl-dimtext" fill="var(--paper)" stroke="none">{d.text}</text>
    </g>
  );
}
