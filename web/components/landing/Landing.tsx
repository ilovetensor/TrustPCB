"use client";
import "./landing.css";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { Curve } from "../../lib/curve";
import { useReducedMotion, reducedMotion } from "../../lib/hooks";
import { FIXTURE_MODE, listProjects } from "../../lib/api";
import { loadPatchSim, scenarioBoard, type RfBundle } from "../../lib/rf-data";
import type { Board, ProjectSummary } from "../../lib/types";
import { PatchBoard3D } from "../rf/PatchBoard3D";
import { PulsePlane3D } from "../rf/WaveField3D";
import { WaveField3D } from "../rf/WaveField3D";
import { TuningGraph } from "../rf/charts";
import { ClassStamp, FixtureBadge, Icon, Logo, SevBar } from "../ui";
import dynamic from "next/dynamic";

const Scene3D = dynamic(() => import("../scene3d/Scene3D").then((m) => m.Scene3D), { ssr: false });
/** Loops the solver's real time-domain pulse frames so the hero keeps radiating. */
function PulseLoop({ rf }: { rf: RfBundle }) {
  const [f, setF] = useState(0);
  useEffect(() => {
    if (!rf.pulse) return;
    if (reducedMotion()) { setF(Math.floor(rf.pulse.frames.length * 0.45)); return; } // a still frame, no loop
    const n = rf.pulse.frames.length + 14;
    const id = setInterval(() => setF((x) => (x + 1) % n), 80);
    return () => clearInterval(id);
  }, [rf]);
  if (!rf.pulse) return null;
  return <PulsePlane3D pulse={rf.pulse} y={rf.pulse.at_mm + 0.3} frame={Math.min(f, rf.pulse.frames.length - 1)} fadeOut={f >= rf.pulse.frames.length ? 1 - (f - rf.pulse.frames.length) / 14 : 1} />;
}
const none = { l: 0, r: 0, t: 0, b: 0 };
const noop = () => {};

const STEPS = [
  ["01", "Upload", "Drop a KiCad project. TrustPCB reads the real geometry of every track, pad and part."],
  ["02", "Baseline", "KiCad rule checks and nine analyzers measure the board in code, with no AI involved."],
  ["03", "Investigate", "An agent forms hypotheses and runs real electromagnetic simulations to test them."],
  ["04", "Mark up", "Every issue is pinned to a real object on the board, with the numbers that prove it."],
];

export function Landing() {
  const calm = useReducedMotion();
  const [rf, setRf] = useState<RfBundle | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [fx, setFx] = useState<{ curve: Curve; curve2: Curve; band: { lo_ghz: number; hi_ghz: number; target_db: number } } | null>(null);
  useEffect(() => { listProjects().then(setProjects).catch(() => setProjects([])); loadPatchSim().then(setRf); }, []);
  // illustrative curves exist only in fixture mode
  useEffect(() => { if (FIXTURE_MODE) void import("../../lib/fixture-api").then((m) => setFx({ curve: m.resolveCurve("base")!, curve2: m.resolveCurve("confirm")!, band: m.TARGET_BAND })); }, []);
  const latest = projects.find((p) => p.latest_review_id);
  const sampleHref = FIXTURE_MODE ? "/reviews/r-0042" : latest ? `/reviews/${latest.latest_review_id}` : null;
  const board: Board | null = useMemo(() => (rf ? scenarioBoard(rf) : null), [rf]);
  const empty = useMemo(() => ({ marks: [] as never[], meta: {} }), []);

  return (
    <main className="ld">
      <section className="ld-hero">
        <div className="ld-scene" aria-hidden>
          <div className="stage-bg" />
          {board && rf && (
            <Scene3D
              bare center={[0, 0.8, 0]} board={board} glbUrl="" marks={empty.marks} meta={empty.meta} selectedId={null} onSelect={noop}
              explode={0} insets={none} hero autoRotate={!calm} showMarks={false} shift={[0.2, 0.02]}
            >
              <PatchBoard3D rf={rf} />
              {rf.fieldXY && <WaveField3D slice={rf.fieldXY} />}
              {rf.fieldXZ && <WaveField3D slice={rf.fieldXZ} />}
              <PulseLoop rf={rf} />
            </Scene3D>
          )}
        </div>
        <div className="ld-frame" aria-hidden>
          {["A", "B", "C", "D", "E"].map((l, i) => (<span key={l} className="ld-zl mono" style={{ top: `${10 + i * 20}%` }}>{l}</span>))}
          {[1, 2, 3, 4, 5, 6, 7, 8].map((n, i) => (<span key={n} className="ld-zn mono" style={{ left: `${6 + i * 12.5}%` }}>{n}</span>))}
        </div>
        <header className="ld-nav">
          <Logo />
          <nav>
            <Link href="/projects">Projects</Link>
            <Link href="/compare">Compare</Link>
            {sampleHref && <Link href={sampleHref}>{FIXTURE_MODE ? "Sample review" : "Latest review"}</Link>}
          </nav>
          <span style={{ flex: 1 }} />
          <FixtureBadge />
          <Link className="btn sm" href="/upload"><Icon name="upload" size={12} /> Upload</Link>
        </header>
        <div className="ld-copy">
          <span className="idx rise" style={{ animationDelay: "100ms" }}>TPCB / INSPECTION BENCH / REV 0.1</span>
          <h1 className="serif">
            <span className="rise l1" style={{ animationDelay: "200ms" }}>Others read your datasheets.</span>
            <span className="rise l2" style={{ animationDelay: "380ms" }}>TrustPCB tests <em>your board.</em></span>
          </h1>
          <p className="rise" style={{ animationDelay: "560ms" }}>
            Upload a KiCad board. Get it marked up like a senior engineer went over the drawing: real simulations, measured numbers, and tested fixes. Nothing is edited. Nothing is guessed.
          </p>
          <div className="ld-cta rise" style={{ animationDelay: "720ms" }}>
            <Link className="btn primary big" href="/upload"><Icon name="upload" size={15} /> Upload a board</Link>
            {sampleHref && <Link className="btn big" href={sampleHref}>{FIXTURE_MODE ? "Watch a sample review" : "Open your latest review"} <Icon name="arrow" size={14} /></Link>}
          </div>
          <div className="ld-labels rise" style={{ animationDelay: "880ms" }}>
            <span className="idx">EVERY CLAIM IS LABELLED</span>
            <ClassStamp cls="measured" /><ClassStamp cls="simulated" /><ClassStamp cls="opinion" /><ClassStamp cls="not_tested" />
          </div>
        </div>
        <div className="ld-fig mono rise" style={{ animationDelay: "1000ms" }}>
          <span className="idx">FIG. 1</span>
          <span>2.5 GHz patch antenna, 60 x 50 mm. Complex field from the solver, phase locked, time slowed to be visible.</span>
          <span className="stamp sim"><i />Real openEMS output</span>
        </div>
        <div className="ld-title hair mono">
          <div><span className="idx">DRAWING</span><b>INSPECTION BENCH</b></div>
          <div><span className="idx">SHEET</span><b>1 / 1</b></div>
          <div><span className="idx">SCALE</span><b>1 : 1</b></div>
          <div><span className="idx">REV</span><b>A</b></div>
        </div>
      </section>

      <section className="ld-sec">
        <div className="ld-sec-head"><span className="idx">02 / METHOD</span><h2 className="serif">An inspection, not a rewrite.</h2></div>
        <ol className="ld-steps">
          {STEPS.map(([n, t, d]) => (
            <li key={n} className="hair panel">
              <svg width="30" height="30" viewBox="0 0 30 30" aria-hidden><circle cx="19" cy="11" r="8.5" fill="none" stroke="var(--signal)" strokeWidth="1.3" /><text x="19" y="11" textAnchor="middle" dy="0.34em" className="mono" fontSize="9" fill="var(--paper)">{n}</text><path d="M13 17 3 28h6" fill="none" stroke="var(--signal)" strokeWidth="1.3" /></svg>
              <h3>{t}</h3><p>{d}</p>
            </li>
          ))}
        </ol>
      </section>

      {projects.length > 0 && <section className="ld-sec">
        <div className="ld-sec-head"><span className="idx">03 / {FIXTURE_MODE ? "SAMPLE REVIEWS" : "YOUR BOARDS"}</span><h2 className="serif">{FIXTURE_MODE ? "See finished reviews." : "Pick up where you left off."}</h2></div>
        <div className="ld-cards">
          {projects.slice(0, 3).map((p, i) => (
            <Link key={p.id} href={p.latest_review_id ? `/reviews/${p.latest_review_id}` : `/projects/${p.id}`} className="ld-card hair panel">
              <div className="ld-card-top"><span className="idx">R-{p.latest_review_id.slice(0, 6).toUpperCase()}</span><span className="idx">{p.latest_version}</span></div>
              <h3>{p.name}</h3>
              <p className="dim">{p.kind}</p>
              <div className="ld-card-graph">
                {fx && p.rf !== "n/a" && <TuningGraph curves={i === 1 ? [{ id: "a", label: "a", ...fx.curve2 }] : [{ id: "a", label: "a", ...fx.curve }, { id: "b", label: "b", ...fx.curve2 }]} band={fx.band} height={64} compact />}
                {!fx && <div className="dim mono" style={{ fontSize: 11, padding: "20px 0" }}>{p.rf === "n/a" ? "No radio part detected." : "RF not tested."}</div>}
              </div>
              <div className="ld-card-foot"><SevBar counts={p.counts} size="sm" /><span className="link-arrow mono idx">OPEN</span></div>
            </Link>
          ))}
        </div>
      </section>}

      <footer className="ld-foot">
        <Logo />
        <span className="dim">Open source inspection layer for circuit boards. TrustPCB never edits a design and never outputs modified KiCad or manufacturing files.</span>
      </footer>
    </main>
  );
}
