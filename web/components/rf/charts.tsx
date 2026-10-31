"use client";
import "../board2d/board2d.css";
import { useMemo, useRef, useState } from "react";
import { rgb, FIELD_STOPS } from "../../lib/ramp.mjs";
import type { ZinData } from "../../lib/rf-data";

export interface CurveSpec { id: string; label: string; f_ghz: number[]; s11_db: number[]; color?: string; dashed?: boolean; width?: number; opacity?: number }
interface DipMark { f_ghz: number; db: number; /** stayed put when the geometry changed: not the antenna's resonance */ fixed?: boolean }
interface VLine { f_ghz: number; label: string }
interface Band { lo_ghz: number; hi_ghz: number; target_db: number }

const interp = (xs: number[], ys: number[], x: number) => {
  if (x <= xs[0]) return ys[0];
  for (let i = 1; i < xs.length; i++) if (x <= xs[i]) { const k = (x - xs[i - 1]) / (xs[i] - xs[i - 1] || 1); return ys[i - 1] + (ys[i] - ys[i - 1]) * k; }
  return ys[ys.length - 1];
};
export const minOf = (c: { f_ghz: number[]; s11_db: number[] }) => { const i = c.s11_db.indexOf(Math.min(...c.s11_db)); return { f: c.f_ghz[i], db: c.s11_db[i] }; };

/** Tuning graph: S11 over frequency, band shading, target line, draggable marker. */
export function TuningGraph({ curves, band, marker, onMarker, height = 240, compact = false, yMin = -30, dips, vlines, markMinima = true }: {
  curves: CurveSpec[]; band?: Band | null; marker?: number | null; onMarker?: (f: number) => void; height?: number; compact?: boolean; yMin?: number;
  /** every S11 dip of the main curve, numbered on the graph */ dips?: DipMark[]; /** vertical reference lines such as the first series resonance */ vlines?: VLine[]; markMinima?: boolean;
}) {
  const W = compact ? 140 : 400;
  const m = compact ? { l: 4, r: 4, t: 4, b: 4 } : { l: 44, r: 14, t: 14, b: 30 };
  const f0 = Math.min(...curves.map((c) => c.f_ghz[0])), f1 = Math.max(...curves.map((c) => c.f_ghz[c.f_ghz.length - 1]));
  const x = (f: number) => m.l + ((f - f0) / (f1 - f0)) * (W - m.l - m.r);
  const y = (d: number) => m.t + ((0 - d) / (0 - yMin)) * (height - m.t - m.b);
  const svg = useRef<SVGSVGElement>(null);
  const [drag, setDrag] = useState(false);
  const fromEvent = (e: React.PointerEvent) => {
    const r = svg.current!.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    return Math.min(f1, Math.max(f0, f0 + ((px - m.l) / (W - m.l - m.r)) * (f1 - f0)));
  };
  const main = curves[curves.length - 1];
  const d = (c: CurveSpec) => "M" + c.f_ghz.map((f, i) => `${x(f).toFixed(1)} ${y(c.s11_db[i]).toFixed(1)}`).join("L");
  const ticks = useMemo(() => { const t: number[] = []; for (let f = Math.ceil(f0 * 10) / 10; f <= f1 + 1e-6; f += 0.1) t.push(+f.toFixed(2)); return t; }, [f0, f1]);
  const cols = ["var(--paper-3)", "var(--paper)", "var(--signal)"];
  return (
    <svg ref={svg} viewBox={`0 0 ${W} ${height}`} width="100%" style={{ display: "block", touchAction: "none" }} role="img" aria-label="Tuning graph, S11 against frequency"
      onPointerDown={(e) => { if (!onMarker) return; (e.currentTarget as SVGElement).setPointerCapture(e.pointerId); setDrag(true); onMarker(fromEvent(e)); }}
      onPointerMove={(e) => { if (drag && onMarker) onMarker(fromEvent(e)); }} onPointerUp={() => setDrag(false)}>
      {band && <rect x={x(band.lo_ghz)} y={m.t} width={x(band.hi_ghz) - x(band.lo_ghz)} height={height - m.t - m.b} fill="var(--signal)" opacity="0.1" />}
      {band && <path d={`M${x(band.lo_ghz)} ${m.t}V${height - m.b}M${x(band.hi_ghz)} ${m.t}V${height - m.b}`} stroke="var(--signal)" strokeWidth="1" strokeDasharray="3 3" opacity="0.7" />}
      {!compact && [0, -5, -10, -15, -20, -25, -30].filter((v) => v >= yMin).map((v) => (
        <g key={v}><line x1={m.l} x2={W - m.r} y1={y(v)} y2={y(v)} stroke="var(--hair)" /><text x={m.l - 6} y={y(v)} textAnchor="end" dy="0.34em" className="rl-t">{v}</text></g>
      ))}
      {band && <line x1={m.l} x2={W - m.r} y1={y(band.target_db)} y2={y(band.target_db)} stroke="var(--signal)" strokeWidth="1" opacity="0.9" />}
      {band && !compact && <text x={W - m.r - 4} y={y(band.target_db) - 5} textAnchor="end" className="rl-t" fill="var(--signal)">Target {band.target_db} dB</text>}
      {!compact && ticks.map((f, i) => (
        <g key={f}>
          <line x1={x(f)} x2={x(f)} y1={height - m.b} y2={height - m.b + (i % 2 === 0 ? 6 : 3)} stroke="var(--paper-3)" />
          {i % 2 === 0 && <text x={x(f)} y={height - m.b + 17} textAnchor="middle" className="rl-t">{f.toFixed(1)}</text>}
        </g>
      ))}
      {!compact && <text x={m.l} y={height - 2} className="rl-t">GHz</text>}
      {!compact && <text x={m.l + 4} y={m.t + 8} className="rl-t">S11 dB</text>}
      {!compact && vlines?.filter((v) => v.f_ghz >= f0 && v.f_ghz <= f1).map((v, i) => (
        <g key={"vl" + i}>
          <line x1={x(v.f_ghz)} x2={x(v.f_ghz)} y1={m.t} y2={height - m.b} stroke="var(--paper-2)" strokeWidth="1" strokeDasharray="1.5 3" />
          <text x={x(v.f_ghz) + 3} y={height - m.b - 5} className="rl-t" fill="var(--paper-2)">{v.label}</text>
        </g>
      ))}
      {curves.map((c, i) => (
        <path key={c.id} d={d(c)} fill="none" stroke={c.color ?? (i === curves.length - 1 ? "var(--paper)" : cols[0])} strokeWidth={c.width ?? (i === curves.length - 1 ? 1.8 : 1.3)} strokeDasharray={c.dashed ?? i < curves.length - 1 ? "4 3" : undefined} opacity={c.opacity} strokeLinejoin="round" />
      ))}
      {!compact && !dips && markMinima && curves.map((c) => { const mn = minOf(c); return (<g key={"mn" + c.id}><circle cx={x(mn.f)} cy={y(mn.db)} r="2.6" fill="var(--stage)" stroke="var(--paper-2)" /></g>); })}
      {!compact && dips?.filter((dp) => dp.f_ghz >= f0 && dp.f_ghz <= f1).map((dp, i) => (
        <g key={"dip" + i}>
          <circle cx={x(dp.f_ghz)} cy={y(dp.db)} r="2.8" fill="var(--stage)" stroke={dp.fixed ? "var(--paper-3)" : "var(--paper)"} strokeWidth="1.2" strokeDasharray={dp.fixed ? "1.6 1.4" : undefined} />
          <text x={x(dp.f_ghz)} y={y(dp.db) + 12} textAnchor="middle" className="rl-t" fill={dp.fixed ? "var(--paper-3)" : "var(--paper)"}>{i + 1}{dp.fixed ? " fixed" : ""}</text>
        </g>
      ))}
      {!compact && marker != null && (
        <g>
          <line x1={x(marker)} x2={x(marker)} y1={m.t} y2={height - m.b} stroke="var(--paper)" strokeWidth="1" />
          <circle cx={x(marker)} cy={y(interp(main.f_ghz, main.s11_db, marker))} r="4" fill="var(--signal)" stroke="var(--stage)" strokeWidth="1.5" />
          <g transform={`translate(${x(marker) > (m.l + W - m.r) / 2 ? m.l + 8 : W - m.r - 112} ${y(-13)})`}>
            <rect width="108" height="34" rx="9" fill="var(--panel-solid)" />
            <text x="6" y="14" className="rl-t" fill="var(--paper)">{marker.toFixed(3)} GHz</text>
            <text x="6" y="27" className="rl-t" fill="var(--signal)">{interp(main.f_ghz, main.s11_db, marker).toFixed(1)} dB</text>
          </g>
        </g>
      )}
    </svg>
  );
}

/** Smith chart (50 ohm) with the frequency path, band highlighted. */
export function SmithChart({ zin, band, marker, size = 320, second }: { zin: ZinData; band: Band; marker?: number | null; size?: number; /** a second path, drawn dashed (as built over antenna only) */ second?: ZinData | null }) {
  const c = size / 2, R = size / 2 - 14;
  const g = zin.f_ghz.map((f, i) => {
    const zr = zin.z_re[i] / 50, zi = zin.z_im[i] / 50;
    const den = (zr + 1) ** 2 + zi ** 2;
    return { f, x: ((zr * zr + zi * zi - 1) / den) * R + c, y: -((2 * zi) / den) * R + c };
  });
  const g2 = second ? second.f_ghz.map((f, i) => {
    const zr = second.z_re[i] / 50, zi = second.z_im[i] / 50;
    const den = (zr + 1) ** 2 + zi ** 2;
    return { f, x: ((zr * zr + zi * zi - 1) / den) * R + c, y: -((2 * zi) / den) * R + c };
  }) : null;
  const rc = [0.2, 0.5, 1, 2, 5];
  const xc = [0.2, 0.5, 1, 2, 5];
  const pt = (arr: typeof g, a: number, b: number) => arr.filter((p) => p.f >= a && p.f <= b);
  const seg = (arr: typeof g) => (arr.length ? "M" + arr.map((p) => `${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join("L") : "");
  const inBand = pt(g, band.lo_ghz, band.hi_ghz);
  const mk = marker != null ? g.reduce((a, b) => (Math.abs(b.f - marker) < Math.abs(a.f - marker) ? b : a), g[0]) : null;
  return (
    <svg viewBox={`0 0 ${size} ${size}`} width="100%" style={{ display: "block", maxWidth: size }} role="img" aria-label="Smith chart">
      <defs><clipPath id="sm-clip"><circle cx={c} cy={c} r={R} /></clipPath></defs>
      <circle cx={c} cy={c} r={R} fill="none" stroke="var(--hair-2)" />
      <g clipPath="url(#sm-clip)" fill="none" stroke="var(--hair)" strokeWidth="1">
        <line x1={c - R} x2={c + R} y1={c} y2={c} />
        {rc.map((r) => (<circle key={r} cx={c + (r / (1 + r)) * R} cy={c} r={R / (1 + r)} />))}
        {xc.flatMap((x) => [1, -1].map((s) => (<circle key={`${x}${s}`} cx={c + R} cy={c - (s * R) / x} r={R / x} />)))}
      </g>
      <circle cx={c} cy={c} r={R * 0.3333} fill="none" stroke="var(--paper-3)" strokeDasharray="2 3" />
      <text x={c + R * 0.3333 + 3} y={c - 4} className="rl-t">VSWR 2</text>
      {[0.2, 0.5, 1, 2, 5].map((r) => (<text key={r} x={c + ((r - 1) / (r + 1)) * R + 2} y={c + 10} className="rl-t" textAnchor="middle" opacity="0.7">{r}</text>))}
      {g.slice(1).map((p, i) => (<line key={i} x1={g[i].x} y1={g[i].y} x2={p.x} y2={p.y} stroke={rgb(FIELD_STOPS, 0.15 + 0.8 * (i / g.length))} strokeWidth="1.8" opacity={p.f >= band.lo_ghz && p.f <= band.hi_ghz ? 1 : 0.55} />))}
      {g2 && <path d={seg(g2)} fill="none" stroke="var(--paper-2)" strokeWidth="1.2" strokeDasharray="4 3" opacity="0.9" />}
      <path d={seg(inBand)} fill="none" stroke="var(--paper)" strokeWidth="3.4" strokeLinecap="round" opacity="0.9" />
      {g.length > 0 && <circle cx={g[0].x} cy={g[0].y} r="2.5" fill="var(--paper-3)" />}
      {mk && (<g><circle cx={mk.x} cy={mk.y} r="5" fill="var(--signal)" stroke="var(--stage)" strokeWidth="1.5" /><text x={mk.x + 8} y={mk.y - 8} className="rl-t" fill="var(--paper)">{mk.f.toFixed(3)}</text></g>)}
    </svg>
  );
}

