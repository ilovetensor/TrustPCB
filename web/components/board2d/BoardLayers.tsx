"use client";
import { memo, useMemo } from "react";
import { arcPoints } from "../../lib/geometry.mjs";
import type { Zone as FunctionZone } from "../../lib/api";
import type { Board, Pt, Ring } from "../../lib/types";
import type { LayerState } from "./layers";
import type { SceneMarkup } from "./useScene";

const f = (n: number) => +n.toFixed(3);
const ringD = (r: Ring) => "M" + r.map((p) => `${f(p[0])} ${f(p[1])}`).join("L") + "Z";
const lineD = (pts: Pt[]) => "M" + pts.map((p) => `${f(p[0])} ${f(p[1])}`).join("L");

const ROLE_COLORS: Record<string, string> = {
  rf: "#ff9a5c", power: "#f2d36b", usb: "#6fd6e8", digital: "#a99cff", analog: "#7fe0a0", clock: "#f08ad0",
  connector: "#9fb6d8", protection: "#e88a8a", sensor: "#8fe0c8", radio: "#ff9a5c",
};
export const roleColor = (role: string) => ROLE_COLORS[role] ?? "#9d9bc0";

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
    // fixture boards carry their own silk; live boards use the scene SVG layers
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
    // plated through holes and vias as one path each
    const viaD = board.vias.map((v) => `M${f(v.x - v.diameter / 2)} ${f(v.y)}a${f(v.diameter / 2)} ${f(v.diameter / 2)} 0 1 0 ${f(v.diameter)} 0a${f(v.diameter / 2)} ${f(v.diameter / 2)} 0 1 0 ${f(-v.diameter)} 0Z`).join("");
    const viaHoleD = board.vias.map((v) => `M${f(v.x - v.drill / 2)} ${f(v.y)}a${f(v.drill / 2)} ${f(v.drill / 2)} 0 1 0 ${f(v.drill)} 0a${f(v.drill / 2)} ${f(v.drill / 2)} 0 1 0 ${f(-v.drill)} 0Z`).join("");
    return { outline, trackGroups, padD, holes, zoneD, silkD, silkText, silkCircles, viaD, viaHoleD };
  }, [board]);
}

export const BoardLayers = memo(function BoardLayers({ board, layers, scene, functionZones = [] }: {
  board: Board; layers: LayerState; scene?: SceneMarkup | null; functionZones?: FunctionZone[];
}) {
  const g = useGeometry(board);
  const [x0, y0, x1, y1] = board.bbox;
  return (
    <g className="bl">
      <defs>
        <pattern id="hatch-top" patternUnits="userSpaceOnUse" width="0.7" height="0.7" patternTransform="rotate(45)">
          <path d="M0 0H0.7" stroke="var(--bl-fcu)" strokeWidth="0.09" />
        </pattern>
        <pattern id="hatch-bot" patternUnits="userSpaceOnUse" width="0.9" height="0.9" patternTransform="rotate(-45)">
          <path d="M0 0H0.9" stroke="var(--bl-bcu)" strokeWidth="0.09" />
        </pattern>
        <pattern id="hatch-keep" patternUnits="userSpaceOnUse" width="1.1" height="1.1" patternTransform="rotate(45)">
          <path d="M0 0H1.1" stroke="#a78bfa" strokeWidth="0.07" />
        </pattern>
        <filter id="bl-glow" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation="1.6" /></filter>
      </defs>
      {/* soft light under the board so its edge lifts off the stage */}
      <path d={g.outline} className="bl-glow" filter="url(#bl-glow)" />
      <path d={g.outline} className="bl-substrate" />
      <path d={g.outline} className="bl-substrate-inner" />
      {/* bottom copper */}
      {layers.bottom && (
        <g className="bl-bot">
          {layers.zones && g.zoneD["B.Cu"] && (<>
            <path d={g.zoneD["B.Cu"]} fill="var(--bl-bcu)" fillRule="evenodd" opacity="0.1" />
            <path d={g.zoneD["B.Cu"]} fill="url(#hatch-bot)" fillRule="evenodd" opacity="0.16" />
          </>)}
          {[...g.trackGroups["B.Cu"]].map(([w, d]) => (<path key={w} d={d} stroke="var(--bl-bcu)" strokeWidth={w} fill="none" strokeLinecap="round" strokeLinejoin="round" opacity="0.88" />))}
          {layers.pads && g.padD["B.Cu"] && <path d={g.padD["B.Cu"]} fill="var(--bl-pad-b)" />}
          {layers.silk && scene?.silkBottom && <g className="sc sc-silk sc-back" dangerouslySetInnerHTML={{ __html: scene.silkBottom }} />}
        </g>
      )}
      {/* top copper */}
      {layers.top && (
        <g className="bl-top">
          {layers.zones && g.zoneD["F.Cu"] && (<>
            <path d={g.zoneD["F.Cu"]} fill="var(--bl-fcu)" fillRule="evenodd" opacity="0.1" />
            <path d={g.zoneD["F.Cu"]} fill="url(#hatch-top)" fillRule="evenodd" opacity="0.2" />
          </>)}
          {[...g.trackGroups["F.Cu"]].map(([w, d]) => (<path key={w} d={d} stroke="var(--bl-fcu)" strokeWidth={w} fill="none" strokeLinecap="round" strokeLinejoin="round" />))}
          {layers.pads && <path d={g.padD["F.Cu"]} fill="var(--bl-pad)" />}
        </g>
      )}
      {layers.vias && g.viaD && (
        <g className="bl-vias">
          <path d={g.viaD} fill="var(--bl-via)" />
          <path d={g.viaHoleD} fill="var(--bl-hole)" />
        </g>
      )}
      {layers.pads && g.holes.map((h, i) => (<circle key={i} cx={h.x} cy={h.y} r={h.r} fill="var(--bl-hole)" />))}
      {layers.silk && (
        <g className="bl-silk">
          {scene?.silkTop && <g className="sc sc-silk" dangerouslySetInnerHTML={{ __html: scene.silkTop }} />}
          {scene?.refsTop && <g className="sc sc-refs" dangerouslySetInnerHTML={{ __html: scene.refsTop }} />}
          {!scene && (
            <g fill="none" stroke="var(--bl-silk)" strokeWidth="0.12" strokeLinecap="round">
              <path d={g.silkD} />
              {g.silkCircles.map((c, i) => (<circle key={i} cx={c.c[0]} cy={c.c[1]} r={c.r} />))}
              {g.silkText.map((t, i) => t.kind === "text" && (
                <text key={i} x={t.x} y={t.y} fontSize={t.size} fill="var(--bl-silk)" stroke="none" textAnchor="middle" style={{ font: `500 ${t.size}px var(--font-mono)` }}>{t.text}</text>
              ))}
            </g>
          )}
        </g>
      )}
      {layers.regions && functionZones.length > 0 && (
        <g className="bl-fzones" fill="none">
          {functionZones.map((z) => (
            <path key={`${z.role}-${z.index}`} d={ringD(z.ring)} fill={roleColor(z.role)} fillOpacity="0.05" stroke={roleColor(z.role)} strokeOpacity="0.4" strokeWidth="0.1" strokeDasharray="0.5 0.35" strokeLinejoin="round" />
          ))}
        </g>
      )}
      {board.keepouts.map((k) => (
        <g key={k.id} className="bl-keep">
          <path d={ringD(k.ring)} fill="url(#hatch-keep)" opacity="0.5" />
          <path d={ringD(k.ring)} fill="none" stroke="#a78bfa" strokeWidth="1.2px" strokeDasharray="6 4" vectorEffect="non-scaling-stroke" opacity="0.85" />
        </g>
      ))}
      <path d={g.outline} className="bl-edge" vectorEffect="non-scaling-stroke" />
      {/* origin datum drawn on the sheet */}
      <g className="bl-sheet" stroke="var(--paper-3)" strokeWidth="1" vectorEffect="non-scaling-stroke">
        <Axis x0={x0} y0={y0} x1={x1} y1={y1} />
      </g>
    </g>
  );
});

/** Zone role labels, drawn in screen-sized text so they stay readable at any zoom. */
export function ZoneLabels({ zones, px, show }: { zones: FunctionZone[]; px: (n: number) => number; show: boolean }) {
  if (!show || !zones.length) return null;
  return (
    <g className="bl-zlabels" pointerEvents="none">
      {zones.map((z) => {
        const xs = z.ring.map((p) => p[0]), ys = z.ring.map((p) => p[1]);
        const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = Math.min(...ys);
        return <text key={`${z.role}-${z.index}`} x={cx} y={cy - px(4)} textAnchor="middle" fontSize={px(9)} fill={roleColor(z.role)} fillOpacity="0.8" style={{ font: `500 ${px(9)}px var(--font-mono)`, letterSpacing: "0.06em", textTransform: "uppercase" }}>{z.role}</text>;
      })}
    </g>
  );
}

function Axis({ x0, y0, x1, y1 }: { x0: number; y0: number; x1: number; y1: number }) {
  return (
    <g>
      <path d={`M${x0 - 1.6} ${y0}H${x0 + 1.6}M${x0} ${y0 - 1.6}V${y0 + 1.6}`} vectorEffect="non-scaling-stroke" />
      <path d={`M${x1 - 1.6} ${y1}H${x1 + 1.6}M${x1} ${y1 - 1.6}V${y1 + 1.6}`} vectorEffect="non-scaling-stroke" opacity="0.5" />
    </g>
  );
}
