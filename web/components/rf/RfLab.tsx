"use client";
import "./rf.css";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { loadPatchSim, scenarioBoard, type RfBundle } from "../../lib/rf-data";
import type { Board } from "../../lib/types";
import { PatchBoard3D } from "./PatchBoard3D";
import { Board2D } from "../board2d/Board2D";
import { DEFAULT_LAYERS } from "../board2d/layers";
import { Icon, Logo, Panel, FixtureBadge } from "../ui";
import { CurrentCanvas2D } from "./FieldCanvas2D";
import { PulsePlane3D, WaveField3D } from "./WaveField3D";
import { RadiationBalloon } from "./RadiationBalloon";
import { SmithChart, TuningGraph, minOf, type CurveSpec } from "./charts";
import { cssGradient, FIELD_STOPS, SIGNED_STOPS } from "../../lib/ramp.mjs";
import { useNarrow } from "../../lib/hooks";

const Scene3D = dynamic(() => import("../scene3d/Scene3D").then((m) => m.Scene3D), { ssr: false });
type View = "wave" | "pulse" | "pattern" | "current";
const noop = () => {};
const VIEW3D = { az: 0.55, el: 0.62, dist: 235 };

function bandwidth(c: { f_ghz: number[]; s11_db: number[] }, thr = -10) {
  const mn = minOf(c);
  if (mn.db > thr) return null;
  let i = c.f_ghz.indexOf(mn.f), lo = i, hi = i;
  while (lo > 0 && c.s11_db[lo - 1] <= thr) lo--;
  while (hi < c.f_ghz.length - 1 && c.s11_db[hi + 1] <= thr) hi++;
  return { lo: c.f_ghz[lo], hi: c.f_ghz[hi] };
}

export function RfLab({ reviewId }: { reviewId: string }) {
  const [rf, setRf] = useState<RfBundle | null | undefined>(undefined);
  const [view, setView] = useState<View>("wave");
  const [marker, setMarker] = useState<number | null>(null);
  const [paused, setPaused] = useState(false);
  const [frame, setFrame] = useState(0);
  const [pulsePlay, setPulsePlay] = useState(true);
  const narrow = useNarrow();

  useEffect(() => { loadPatchSim().then((b) => { setRf(b); if (b) setMarker(minOf(b.s11).f); }); }, []);
  const board: Board | null = useMemo(() => (rf ? scenarioBoard(rf) : null), [rf]);

  useEffect(() => {
    if (view !== "pulse" || !rf?.pulse || !pulsePlay) return;
    const n = rf.pulse.frames.length;
    const id = setInterval(() => setFrame((f) => { if (f >= n - 1) { setPulsePlay(false); return n - 1; } return f + 1; }), 90);
    return () => clearInterval(id);
  }, [view, rf, pulsePlay]);

  const curves: CurveSpec[] = useMemo(() => {
    if (!rf) return [];
    return [...(rf.overlays ?? []).map((o) => ({ id: o.id, label: o.label, ...o.s11 })), { id: "base", label: "Base", ...rf.s11 }];
  }, [rf]);
  const insets = narrow ? { l: 0, r: 0, t: 0, b: 0 } : { l: 340, r: 430, t: 70, b: 20 };
  const isFx = false;
  const mn = rf ? minOf(rf.s11) : null;
  const bw = rf ? bandwidth(rf.s11) : null;
  const inBand = rf && mn ? mn.f >= rf.band.lo_ghz && mn.f <= rf.band.hi_ghz && mn.db <= rf.band.target_db : false;

  const stage = board && rf ? (
    view === "current" ? (
      <div className="rf-stage"><div className="stage-bg" />
        <Board2D
          board={board} marks={[]} meta={{}} selectedId={null} onSelect={noop} layers={{ ...DEFAULT_LAYERS, issues: false, zones: false, silk: false }}
          insets={insets} overlay={() => (rf.current ? <CurrentCanvas2D data={rf.current} /> : null)} focus={{ bbox: [-32, -27, 32, 27], nonce: 1 }}
        />
      </div>
    ) : (
      <div className="rf-stage"><div className="stage-bg" />
        <Scene3D bare center={[0, 0.8, 0]} board={board} glbUrl="" marks={[]} meta={{}} selectedId={null} onSelect={noop} explode={0} insets={insets} showMarks={false} boardDims={false} autoRotate={view === "pattern"} view3d={VIEW3D}>
          <PatchBoard3D rf={rf} />
          {view === "wave" && rf.fieldXY && <WaveField3D slice={rf.fieldXY} paused={paused} />}
          {view === "wave" && rf.fieldXZ && <WaveField3D slice={rf.fieldXZ} paused={paused} />}
          {view === "pulse" && rf.pulse && <PulsePlane3D pulse={rf.pulse} y={rf.pulse.at_mm} frame={frame} />}
          {view === "pattern" && rf.pattern && <RadiationBalloon pattern={rf.pattern} center={[0, 1.6, 0]} scale={30} />}
        </Scene3D>
      </div>
    )
  ) : <div className="rf-stage"><div className="stage-bg" /></div>;

  const controls = (
    <Panel className="rf-ctl" idx="LAB" title="Simulation view" delay={100} bodyClass="rf-body">
      <div className="seg" role="tablist" aria-label="RF view" style={{ display: "flex" }}>
        {([["wave", "Field"], ["pulse", "Pulse"], ["pattern", "Pattern"], ["current", "Current"]] as [View, string][]).map(([k, l]) => (
          <button key={k} style={{ flex: 1 }} role="tab" aria-selected={view === k} onClick={() => { setView(k); if (k === "pulse") { setFrame(0); setPulsePlay(true); } }}>{l}</button>
        ))}
      </div>
      <div className="rf-legend">
        {view === "wave" && (<><span className="idx">FIELD, INSTANTANEOUS</span><div className="rf-ramp" style={{ background: cssGradient(SIGNED_STOPS) }} /><div className="rf-ramp-l mono"><span>negative</span><span>0</span><span>positive</span></div>
          <p className="dim">Complex solver field at {rf ? (rf.f0_hz / 1e9).toFixed(2) : ""} GHz (the scenario frequency, not the resonance), rotated in phase. Time is slowed so the wave is visible.</p>
          <button className="btn sm" onClick={() => setPaused(!paused)}><Icon name={paused ? "play" : "pause"} size={11} /> {paused ? "Play" : "Pause"}</button></>)}
        {view === "pulse" && (<><span className="idx">PULSE LEAVING THE ANTENNA, |E|</span><div className="rf-ramp" style={{ background: cssGradient(FIELD_STOPS) }} /><div className="rf-ramp-l mono"><span>0</span><span>peak</span></div>
          {rf?.pulse ? (<><input type="range" min={0} max={rf.pulse.frames.length - 1} value={frame} onChange={(e) => { setPulsePlay(false); setFrame(+e.target.value); }} aria-label="Pulse frame" style={{ width: "100%", accentColor: "var(--signal)" }} />
            <button className="btn sm" onClick={() => { setFrame(0); setPulsePlay(true); }}><Icon name="rotate" size={11} /> Play once</button></>) : <p className="dim">No pulse data in this result.</p>}</>)}
        {view === "pattern" && (<><span className="idx">RADIATION SHAPE, DIRECTIVITY</span><div className="rf-ramp" style={{ background: cssGradient(FIELD_STOPS) }} /><div className="rf-ramp-l mono"><span>{rf?.pattern?.floor_dbi ?? -30} dBi</span><span>{rf?.pattern ? `${rf.pattern.dmax_dbi.toFixed(1)} dBi max` : ""}</span></div>
          <p className="dim">Balloon mesh from far-field data. Radius and colour follow strength. The bright dot marks the maximum at theta {rf?.pattern?.dmax_theta_deg} deg, phi {rf?.pattern?.dmax_phi_deg} deg.</p></>)}
        {view === "current" && (<><span className="idx">SURFACE CURRENT ON COPPER, |Js|</span><div className="rf-ramp" style={{ background: cssGradient(FIELD_STOPS) }} /><div className="rf-ramp-l mono"><span>0</span><span>99.5th pct</span></div>
          <p className="dim">Heat colours show where the antenna current flows. Edges can exceed the scale.</p></>)}
      </div>
      <dl className="kv rf-kv">
        <dt>Resonance</dt><dd className="num">{mn ? `${mn.f.toFixed(3)} GHz` : "-"}</dd>
        <dt>Best S11</dt><dd className="num">{mn ? `${mn.db.toFixed(1)} dB` : "-"}</dd>
        <dt>-10 dB band</dt><dd className="num">{bw ? `${bw.lo.toFixed(3)} to ${bw.hi.toFixed(3)} GHz` : "Never reaches -10 dB"}</dd>
        <dt>Target</dt><dd><span className={`stamp ${inBand ? "solid" : "sig"}`}>{inBand ? "In band" : "Off target"}</span><div className="dim" style={{ fontSize: 11, marginTop: 4 }}>Scenario f0 {rf ? (rf.f0_hz / 1e9).toFixed(2) : ""} GHz, band shown +/- 50 MHz</div></dd>
        {rf?.pattern && (<><dt>Max gain</dt><dd className="num">{rf.pattern.dmax_dbi.toFixed(1)} dBi</dd></>)}
        <dt>Trust</dt><dd><span className="stamp">{String((rf?.result as { trust?: { status?: string } } | undefined)?.trust?.status ?? "unchecked")}</span></dd>
      </dl>
      <p className="idx rf-note">SIMULATED. NOT A SUBSTITUTE FOR A REAL ANTENNA MEASUREMENT.</p>
    </Panel>
  );
  const charts = (
    <Panel className="rf-charts" idx="RESPONSE" title="Tuning and match" delay={200} bodyClass="rf-body">
      <span className="idx">S11 AND BAND</span>
      {rf ? <TuningGraph curves={curves} band={rf.band} marker={marker} onMarker={setMarker} height={210} /> : <p className="dim">No result.</p>}
      <div className="rf-leg mono">
        {curves.map((c, i) => (<span key={c.id}><i style={{ background: i === curves.length - 1 ? "var(--paper)" : "var(--paper-3)" }} />{c.label}</span>))}
        <span><i style={{ background: "var(--signal)" }} />Band and target</span>
      </div>
      <span className="idx" style={{ marginTop: 6 }}>SMITH CHART, 50 OHM</span>
      {rf && <SmithChart zin={rf.zin} band={rf.band} marker={marker} size={300} />}
    </Panel>
  );

  return (
    <main className="rf">
      {stage}
      <header className="rf-top">
        <Link href="/projects" aria-label="Projects"><Logo size={20} /></Link>
        <span className="idx">/</span>
        <Link href={`/reviews/${reviewId}`} className="idx">REVIEW {reviewId.toUpperCase()}</Link>
        <span className="idx">/</span><b className="rf-title">RF lab</b>
        <span style={{ flex: 1 }} />
        {rf && <span className="stamp sim" title={rf.source}><i />{rf.label}</span>}
        <FixtureBadge />
        <Link className="btn sm" href={`/reviews/${reviewId}`}><Icon name="board" size={12} /> Workspace</Link>
      </header>
      {reviewId !== "r-0040" && rf && (<div className="rf-banner mono"><span className="stamp fixture">Sample data</span> The solver output shown is the bundled 2.5 GHz patch-antenna run, not this review's board.</div>)}
      {rf === null && (<div className="rf-none hair panel"><span className="idx">NOT TESTED</span><p>No simulation result exists for this review, so the RF lab has nothing to show. Nothing here is estimated.</p></div>)}
      {controls}
      {charts}
    </main>
  );
}
