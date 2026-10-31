"use client";
import "../rf/rf.css";
import dynamic from "next/dynamic";
import { useEffect, useMemo, useState } from "react";
import type { Board } from "../../lib/types";
import { RadiationBalloon } from "../rf/RadiationBalloon";
import type { PatternData } from "../../lib/rf-data";
import { bestRun, useRunData, type RunData } from "../../lib/use-run-data";
import { describeOps, dipsOf, isScreen, listSims, liveSource, QUALITY_NOTE, type SimJob, type SimSource } from "../../lib/sims";
import { Panel } from "../ui";
import { TuningGraph, type CurveSpec } from "../rf/charts";

export const A_COLOR = "var(--sev-low)";
export const B_COLOR = "var(--sev-medium)";

/** Both versions' solver runs, listed from their reviews. A side with no review or no runs stays null. */
export function useCompareSims(fromReview: string | null | undefined, toReview: string | null | undefined) {
  const [a, setA] = useState<SimSource | null>(null);
  const [b, setB] = useState<SimSource | null>(null);
  const [done, setDone] = useState(false);
  useEffect(() => {
    let live = true;
    setDone(false);
    const one = (id: string | null | undefined) => (id ? listSims(id).then((j) => liveSource(id, j)).catch(() => null) : Promise.resolve(null));
    Promise.all([one(fromReview), one(toReview)]).then(([x, y]) => { if (live) { setA(x); setB(y); setDone(true); } });
    return () => { live = false; };
  }, [fromReview, toReview]);
  return { a, b, done };
}

const ghz = (v: number) => v.toFixed(3);
const runName = (j: SimJob) => `${j.ref ?? ""} ${j.label ?? j.kind}`.trim();

function Pick({ source, job, onPick, side }: { source: SimSource; job: SimJob | null; onPick: (id: string) => void; side: "A" | "B" }) {
  const options = source.jobs.filter((j) => j.status === "done" && j.files.includes("s11.json"));
  return (
    <label className="cmp-pick">
      <span className="idx">{side}</span>
      <select value={job?.job_id ?? ""} onChange={(e) => onPick(e.target.value)} aria-label={`Run for version ${side}`}>
        {options.map((j) => (<option key={j.job_id} value={j.job_id}>{runName(j)}{isScreen(j) ? " (screen)" : j.quality === "confirm" ? " (confirm)" : ""}</option>))}
      </select>
    </label>
  );
}

/** Tuning curves of the two versions on one graph, with the dips and the honest caveats. */
export function CompareTuning({ sourceA, sourceB, jobA, jobB, onPickA, onPickB, dataA, dataB, nA, nB, band }: {
  sourceA: SimSource; sourceB: SimSource; jobA: SimJob | null; jobB: SimJob | null; onPickA: (id: string) => void; onPickB: (id: string) => void;
  dataA: RunData; dataB: RunData; nA?: number; nB?: number; band: { lo_ghz: number; hi_ghz: number; target_db: number } | null;
}) {
  const ca = dataA.curves?.antenna ?? null, cb = dataB.curves?.antenna ?? null;
  const specs: CurveSpec[] = useMemo(() => {
    const o: CurveSpec[] = [];
    if (ca) o.push({ id: "a", label: `A v${nA}`, f_ghz: ca.f_ghz, s11_db: ca.s11_db, color: A_COLOR, dashed: true, width: 1.6 });
    if (cb) o.push({ id: "b", label: `B v${nB}`, f_ghz: cb.f_ghz, s11_db: cb.s11_db, color: B_COLOR, dashed: false, width: 2 });
    return o;
  }, [ca, cb, nA, nB]);
  const da = dipsOf(dataA.summary, ca).dips, db = dipsOf(dataB.summary, cb).dips;
  const deepest = (d: { f_ghz: number; db: number }[]) => d.reduce<{ f_ghz: number; db: number } | null>((m, x) => (!m || x.db < m.db ? x : m), null);
  const ma = deepest(da), mb = deepest(db);
  const mixed = jobA && jobB && jobA.quality !== jobB.quality;
  const anyScreen = isScreen(jobA) || isScreen(jobB);
  const ra = dataA.summary?.s11?.antenna_only?.band_worst_db, rb = dataB.summary?.s11?.antenna_only?.band_worst_db;
  const opsB = jobB ? describeOps(jobB.whatif_ops, jobB.material) : "";
  return (
    <Panel className="card" idx="RF" title="Tuning, before and after" delay={150} actions={anyScreen ? <span className="stamp" title={QUALITY_NOTE}>Screen, compare only</span> : undefined}>
      <div style={{ padding: 14, display: "grid", gap: 10 }}>
        <div className="cmp-picks"><Pick source={sourceA} job={jobA} onPick={onPickA} side="A" /><Pick source={sourceB} job={jobB} onPick={onPickB} side="B" /></div>
        {specs.length ? <TuningGraph curves={specs} band={band} height={210} dips={undefined} markMinima /> : <p className="dim">No tuning curve for the chosen runs.</p>}
        <div className="rf-leg mono">
          {specs.map((c) => (<span key={c.id}><i style={{ background: c.color }} />{c.label}</span>))}
          {band && <span><i style={{ background: "var(--signal)" }} />Band {band.lo_ghz.toFixed(2)} to {band.hi_ghz.toFixed(2)} GHz</span>}
        </div>
        <table className="rp-tbl cmp-rf-tbl">
          <thead><tr><th /><th>A v{nA}</th><th>B v{nB}</th><th>Change</th></tr></thead>
          <tbody>
            <tr><td className="idx">DEEPEST DIP</td><td className="mono">{ma ? `${ghz(ma.f_ghz)} GHz, ${ma.db.toFixed(1)} dB` : "-"}</td><td className="mono">{mb ? `${ghz(mb.f_ghz)} GHz, ${mb.db.toFixed(1)} dB` : "-"}</td>
              <td className="mono">{ma && mb ? `${mb.f_ghz >= ma.f_ghz ? "+" : "−"}${Math.abs((mb.f_ghz - ma.f_ghz) * 1000).toFixed(0)} MHz` : "-"}</td></tr>
            <tr><td className="idx">DIPS</td><td className="mono">{da.length}</td><td className="mono">{db.length}</td><td /></tr>
            <tr><td className="idx">BAND WORST</td><td className="mono">{ra != null ? `${ra.toFixed(1)} dB` : "-"}</td><td className="mono">{rb != null ? `${rb.toFixed(1)} dB` : "-"}</td><td className="mono">{ra != null && rb != null ? `${rb - ra >= 0 ? "+" : "−"}${Math.abs(rb - ra).toFixed(1)} dB` : "-"}</td></tr>
          </tbody>
        </table>
        {opsB && <p className="dim" style={{ margin: 0, fontSize: 12 }}>B's run changed: {opsB}.</p>}
        {mixed && <p className="rf-warn">A and B are different run qualities ({jobA?.quality} and {jobB?.quality}). Compare the shape and the dip positions, not the depths.</p>}
        <p className="idx" style={{ margin: 0 }}>SIMULATED. NOT A SUBSTITUTE FOR A REAL ANTENNA MEASUREMENT.</p>
      </div>
    </Panel>
  );
}

export { bestRun };

const Scene3D = dynamic(() => import("../scene3d/Scene3D").then((m) => m.Scene3D), { ssr: false });
export const PATTERN_VIEWS = { persp: { az: 0.55, el: 0.6 }, side: { az: Math.PI / 2, el: 0.08 }, top: { az: 0, el: 1.45 } } as const;
export type PatternViewKey = keyof typeof PATTERN_VIEWS;
const noop = () => {};
const none = [] as never[];
const noMeta = {};
const zero = { l: 0, r: 0, t: 0, b: 0 };

function Slab({ board }: { board: Board }) {
  const [x0, y0, x1, y1] = board.bbox;
  return (
    <mesh position={[(x0 + x1) / 2, 0.8, (y0 + y1) / 2]}>
      <boxGeometry args={[x1 - x0, 1.6, y1 - y0]} />
      <meshStandardMaterial color="#14173a" roughness={0.6} metalness={0.15} transparent opacity={0.9} />
    </mesh>
  );
}

/** One version's radiation balloon over its board outline, from a fixed camera so A and B line up under the slider. */
export function PatternLayer({ board, pattern, center, view, refDbi }: { board: Board; pattern: PatternData | null | undefined; center: [number, number, number]; view: PatternViewKey; refDbi: number }) {
  return (
    <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
      <Scene3D bare board={board} glbUrl="" marks={none} meta={noMeta} selectedId={null} onSelect={noop} explode={0} insets={zero} showMarks={false} boardDims={false} view3d={{ ...PATTERN_VIEWS[view], scale: 1.1 }}>
        <Slab board={board} />
        {pattern && <RadiationBalloon pattern={pattern} center={center} scale={22} rangeDb={20} colorDb={10} refDbi={refDbi} />}
      </Scene3D>
    </div>
  );
}
