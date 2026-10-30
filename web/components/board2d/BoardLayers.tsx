"use client";
import { memo, useMemo } from "react";
import { arcPoints } from "../../lib/geometry.mjs";
import type { Board, Pt, Ring } from "../../lib/types";
import type { LayerState } from "./layers";

const f = (n: number) => +n.toFixed(3);
const ringD = (r: Ring) => "M" + r.map((p) => `${f(p[0])} ${f(p[1])}`).join("L") + "Z";
const lineD = (pts: Pt[]) => "M" + pts.map((p) => `${f(p[0])} ${f(p[1])}`).join("L");

function useGeometry(board: Board) {
  return useMemo(() => {
    const outline = board.outline.map(ringD).join("");
    const trackGroups: Record<string, Map<number, string>> = { "F.Cu": new Map(), "B.Cu": new Map() };
    for (const t of board.tracks) {
      const g = trackGroups[t.layer];
      if (!g) continue;
      const pts = t.mid ? (arcPoints(t.start, t.mid, t.end, 10) as Pt[]) : [t.start, t.end];
      g.set(t.width, (g.get(t.width) ?? "") + lineD(pts));
    }
    const padD: Record<string, string> = { "F.Cu": "", "B.Cu": "" };
    const holes: { x: number; y: number; r: number }[] = [];
    for (const p of board.pads) {
      const top = p.layers.includes("F.Cu") || p.layers.includes("*.Cu");
      const bot = p.layers.includes("B.Cu") || p.layers.includes("*.Cu");
      const d = p.shape === "circle" ? `M${f(p.x - p.w / 2)} ${f(p.y)}a${f(p.w / 2)} ${f(p.w / 2)} 0 1 0 ${f(p.w)} 0a${f(p.w / 2)} ${f(p.w / 2)} 0 1 0 ${f(-p.w)} 0Z` : ringD(p.ring);
      if (top) padD["F.Cu"] += d;
      if (bot && !top) padD["B.Cu"] += d;
      if (p.drill) holes.push({ x: p.x, y: p.y, r: p.drill / 2 });
    }
    const zoneD: Record<string, string> = {};
    for (const z of board.zones) for (const [layer, rings] of Object.entries(z.filled)) zoneD[layer] = (zoneD[layer] ?? "") + rings.map(ringD).join("");
    const silk = (board.silk ?? []).filter((s) => s.layer === "F.SilkS");
    let silkD = "";
    const silkText: typeof silk = [];
    const silkCircles: { c: Pt; r: number; w: number }[] = [];
    for (const s of silk) {
      if (s.kind === "line") silkD += lineD(s.pts);
      else if (s.kind === "poly") silkD += lineD(s.pts) + (s.closed ? "Z" : "");
      else if (s.kind === "arc") silkD += lineD(arcPoints(s.pts[0], s.pts[1], s.pts[2], 10) as Pt[]);
      else if (s.kind === "circle") silkCircles.push({ c: s.c, r: s.r, w: s.w });
      else if (s.kind === "text") silkText.push(s);
    }
    return { outline, trackGroups, padD, holes, zoneD, silkD, silkText, silkCircles };
  }, [board]);
}

export const BoardLayers = memo(function BoardLayers({ board, layers }: { board: Board; layers: LayerState }) {
  const g = useGeometry(board);
  const [x0, y0, x1, y1] = board.bbox;
  return (
    <g className="bl">
      <defs>
        <pattern id="hatch-top" patternUnits="userSpaceOnUse" width="0.7" height="0.7" patternTransform="rotate(45)">
          <path d="M0 0H0.7" stroke="var(--cu-top)" strokeWidth="0.09" />
        </pattern>
        <pattern id="hatch-bot" patternUnits="userSpaceOnUse" width="0.9" height="0.9" patternTransform="rotate(-45)">
          <path d="M0 0H0.9" stroke="var(--cu-bot)" strokeWidth="0.09" />
        </pattern>
        <pattern id="hatch-keep" patternUnits="userSpaceOnUse" width="1.1" height="1.1" patternTransform="rotate(45)">
          <path d="M0 0H1.1" stroke="#a78bfa" strokeWidth="0.07" />
        </pattern>
      </defs>
      {/* substrate */}
      <path d={g.outline} className="bl-substrate" />
      <path d={g.outline} className="bl-substrate-inner" />
      {/* bottom copper */}
      {layers.bottom && (
        <g className="bl-bot">
          {layers.zones && g.zoneD["B.Cu"] && <path d={g.zoneD["B.Cu"]} fill="url(#hatch-bot)" fillRule="evenodd" opacity="0.28" />}
          {[...g.trackGroups["B.Cu"]].map(([w, d]) => (<path key={w} d={d} stroke="var(--cu-bot)" strokeWidth={w} fill="none" strokeLinecap="round" strokeLinejoin="round" opacity="0.75" />))}
          {layers.pads && g.padD["B.Cu"] && <path d={g.padD["B.Cu"]} fill="var(--cu-bot)" opacity="0.8" />}
        </g>
      )}
      {/* top copper */}
      {layers.top && (
        <g className="bl-top">
          {layers.zones && g.zoneD["F.Cu"] && <path d={g.zoneD["F.Cu"]} fill="url(#hatch-top)" fillRule="evenodd" opacity="0.4" />}
          {[...g.trackGroups["F.Cu"]].map(([w, d]) => (<path key={w} d={d} stroke="var(--cu-top)" strokeWidth={w} fill="none" strokeLinecap="round" strokeLinejoin="round" opacity="0.94" />))}
          {layers.pads && <path d={g.padD["F.Cu"]} fill="var(--cu-top)" />}
        </g>
      )}
      {layers.vias && (
        <g className="bl-vias">
          {board.vias.map((v) => (
            <g key={v.id}>
              <circle cx={v.x} cy={v.y} r={v.diameter / 2} fill="var(--stage)" stroke="var(--cu-top)" strokeWidth="0.08" />
              <circle cx={v.x} cy={v.y} r={v.drill / 2} fill="var(--stage)" stroke="var(--cu-top)" strokeWidth="0.05" opacity="0.8" />
            </g>
          ))}
        </g>
      )}
      {layers.pads && g.holes.map((h, i) => (<circle key={i} cx={h.x} cy={h.y} r={h.r} fill="var(--stage)" />))}
      {layers.silk && (
        <g className="bl-silk" fill="none" stroke="var(--paper)" strokeWidth="0.12" opacity="0.5" strokeLinecap="round">
          <path d={g.silkD} />
          {g.silkCircles.map((c, i) => (<circle key={i} cx={c.c[0]} cy={c.c[1]} r={c.r} />))}
          {g.silkText.map((t, i) => t.kind === "text" && (
            <text key={i} x={t.x} y={t.y} fontSize={t.size} fill="var(--paper)" stroke="none" textAnchor="middle" style={{ font: `500 ${t.size}px var(--font-mono)` }}>{t.text}</text>
          ))}
        </g>
      )}
      {board.keepouts.map((k) => (
        <g key={k.id} className="bl-keep">
          <path d={ringD(k.ring)} fill="url(#hatch-keep)" opacity="0.55" />
          <path d={ringD(k.ring)} fill="none" stroke="#a78bfa" strokeWidth="1.2px" strokeDasharray="6 4" vectorEffect="non-scaling-stroke" opacity="0.85" />
        </g>
      ))}
      <path d={g.outline} className="bl-edge" vectorEffect="non-scaling-stroke" />
      {/* corner dimensions drawn on the sheet */}
      <g className="bl-sheet" stroke="var(--paper-3)" strokeWidth="1" vectorEffect="non-scaling-stroke">
        <Axis x0={x0} y0={y0} x1={x1} y1={y1} />
      </g>
    </g>
  );
});

function Axis({ x0, y0, x1, y1 }: { x0: number; y0: number; x1: number; y1: number }) {
  // origin marker at board (0,0): a small crosshair, as on a drawing datum
  return (
    <g>
      <path d={`M${x0 - 1.6} ${y0}H${x0 + 1.6}M${x0} ${y0 - 1.6}V${y0 + 1.6}`} vectorEffect="non-scaling-stroke" />
      <path d={`M${x1 - 1.6} ${y1}H${x1 + 1.6}M${x1} ${y1 - 1.6}V${y1 + 1.6}`} vectorEffect="non-scaling-stroke" opacity="0.5" />
    </g>
  );
}
