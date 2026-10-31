"use client";
import { useRef, useState } from "react";
import { fmtNum, legendTicks } from "../../lib/overlay-math.mjs";

export interface Line { id: string; label: string; x: number[]; y: number[]; color: string; dashed?: boolean; width?: number; opacity?: number }
interface Props {
  lines: Line[]; xUnit: string; yUnit: string; xLabel: string; yLabel: string; height?: number; ariaLabel: string;
  /** a horizontal target with a tolerance band, in y units */ target?: { y: number; tolPct?: number; label?: string } | null;
  yRange?: [number, number] | null;
}

const interp = (xs: number[], ys: number[], x: number) => {
  if (!xs.length) return NaN;
  if (x <= xs[0]) return ys[0];
  for (let i = 1; i < xs.length; i++) if (x <= xs[i]) { const k = (x - xs[i - 1]) / (xs[i] - xs[i - 1] || 1); return ys[i - 1] + (ys[i] - ys[i - 1]) * k; }
  return ys[ys.length - 1];
};

/** A line chart in the style of the tuning graph: hairline grid, small numerals, a target band, a marker that follows the pointer. */
export function LineChart({ lines, xUnit, yUnit, xLabel, yLabel, height = 190, ariaLabel, target = null, yRange = null }: Props) {
  const W = 400, m = { l: 44, r: 12, t: 14, b: 30 };
  const svg = useRef<SVGSVGElement>(null);
  const [at, setAt] = useState<number | null>(null);
  const all = lines.flatMap((l) => l.y).filter(Number.isFinite);
  const xs = lines.flatMap((l) => l.x);
  if (!lines.length || !all.length || !xs.length) return <p className="dim">No curve for this run.</p>;
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let lo = Math.min(...all, target ? target.y * (1 - (target.tolPct ?? 0) / 100) : Infinity), hi = Math.max(...all, target ? target.y * (1 + (target.tolPct ?? 0) / 100) : -Infinity);
  if (yRange) [lo, hi] = yRange;
  const pad = (hi - lo) * 0.12 || 1;
  lo -= pad; hi += pad;
  const X = (v: number) => m.l + ((v - x0) / (x1 - x0 || 1)) * (W - m.l - m.r);
  const Y = (v: number) => m.t + ((hi - v) / (hi - lo)) * (height - m.t - m.b);
  const yt = legendTicks(lo, hi, 4), xt = legendTicks(x0, x1, 5);
  const path = (l: Line) => "M" + l.x.map((v, i) => `${X(v).toFixed(1)} ${Y(l.y[i]).toFixed(1)}`).join("L");
  const fromEvent = (e: React.PointerEvent) => {
    const r = svg.current!.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    return Math.min(x1, Math.max(x0, x0 + ((px - m.l) / (W - m.l - m.r)) * (x1 - x0)));
  };
  return (
    <svg ref={svg} viewBox={`0 0 ${W} ${height}`} width="100%" style={{ display: "block", touchAction: "pan-y" }} role="img" aria-label={ariaLabel}
      onPointerMove={(e) => setAt(fromEvent(e))} onPointerDown={(e) => setAt(fromEvent(e))} onPointerLeave={(e) => { if (e.pointerType === "mouse") setAt(null); }}>
      {target && target.tolPct != null && <rect x={m.l} width={W - m.l - m.r} y={Y(target.y * (1 + target.tolPct / 100))} height={Math.max(1, Y(target.y * (1 - target.tolPct / 100)) - Y(target.y * (1 + target.tolPct / 100)))} fill="var(--signal)" opacity="0.1" />}
      {yt.map((v) => (<g key={`y${v}`}><line x1={m.l} x2={W - m.r} y1={Y(v)} y2={Y(v)} stroke="var(--hair)" /><text x={m.l - 6} y={Y(v)} textAnchor="end" dy="0.34em" className="rl-t">{fmtNum(v)}</text></g>))}
      {target && <line x1={m.l} x2={W - m.r} y1={Y(target.y)} y2={Y(target.y)} stroke="var(--signal)" strokeWidth="1" opacity="0.9" />}
      {target && <text x={W - m.r - 4} y={Y(target.y) - 5} textAnchor="end" className="rl-t" fill="var(--signal)">{target.label ?? `Target ${fmtNum(target.y)} ${yUnit}`}</text>}
      {xt.map((v) => (<g key={`x${v}`}><line x1={X(v)} x2={X(v)} y1={height - m.b} y2={height - m.b + 4} stroke="var(--paper-3)" /><text x={X(v)} y={height - m.b + 15} textAnchor="middle" className="rl-t">{fmtNum(v)}</text></g>))}
      <text x={m.l} y={height - 2} className="rl-t">{xLabel}, {xUnit}</text>
      <text x={m.l + 4} y={m.t + 8} className="rl-t">{yLabel}, {yUnit}</text>
      {lines.map((l) => (<path key={l.id} d={path(l)} fill="none" stroke={l.color} strokeWidth={l.width ?? 1.8} strokeDasharray={l.dashed ? "4 3" : undefined} opacity={l.opacity} strokeLinejoin="round" />))}
      {at != null && (
        <g>
          <line x1={X(at)} x2={X(at)} y1={m.t} y2={height - m.b} stroke="var(--paper)" strokeWidth="1" />
          {lines.map((l) => (<circle key={l.id} cx={X(at)} cy={Y(interp(l.x, l.y, at))} r="3.6" fill={l.dashed ? "var(--stage)" : "var(--signal)"} stroke={l.dashed ? l.color : "var(--stage)"} strokeWidth="1.5" />))}
          <g transform={`translate(${X(at) > (m.l + W - m.r) / 2 ? m.l + 8 : W - m.r - 118} ${m.t + 2})`}>
            <rect width="114" height={14 + 13 * lines.length} rx="9" fill="var(--panel-solid)" />
            <text x="8" y="13" className="rl-t" fill="var(--paper-3)">{fmtNum(at)} {xUnit}</text>
            {lines.slice().reverse().map((l, i) => (<text key={l.id} x="8" y={26 + i * 13} className="rl-t" fill={l.dashed ? "var(--paper-2)" : "var(--paper)"}>{fmtNum(interp(l.x, l.y, at))} {yUnit}{lines.length > 1 ? ` ${l.label}` : ""}</text>))}
          </g>
        </g>
      )}
    </svg>
  );
}
