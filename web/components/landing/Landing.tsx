"use client";
import "./landing.css";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import type { Curve } from "../../lib/curve";
import { useReducedMotion, reducedMotion } from "../../lib/hooks";
import { FIXTURE_MODE, listProjects } from "../../lib/api";
import { loadFixtures } from "../../lib/fixture-loader";
import { loadPatchSim, scenarioBoard, type PulseData, type RfBundle } from "../../lib/rf-data";
import { loadField, loadPulse, NRF_BOARD, nrfSampleSource, type Fields } from "../../lib/sims";
import { normaliseBoard } from "../../lib/api";
import type { Board, ProjectSummary } from "../../lib/types";
import { PatchBoard3D } from "../rf/PatchBoard3D";
import { PulsePlane3D } from "../rf/WaveField3D";
import { WaveField3D } from "../rf/WaveField3D";
import { TuningGraph } from "../rf/charts";
import { ClassStamp, DemoNotice, FixtureBadge, Icon, Logo, SevBar } from "../ui";
import { useAppEnv } from "../../lib/demo";
import { PowerPair } from "../rf/PowerPair";
import { acceptedPct } from "../../lib/sims";
import dynamic from "next/dynamic";

const Scene3D = dynamic(() => import("../scene3d/Scene3D").then((m) => m.Scene3D), { ssr: false });
/** Loops the solver's real time-domain pulse frames so the hero keeps radiating. */
function PulseLoop({ pulse }: { pulse: PulseData | null | undefined }) {
  const [f, setF] = useState(0);
  useEffect(() => {
    if (!pulse) return;
    if (reducedMotion()) { setF(Math.floor(pulse.frames.length * 0.45)); return; } // a still frame, no loop
    const n = pulse.frames.length + 14;
    const id = setInterval(() => setF((x) => (x + 1) % n), 80);
    return () => clearInterval(id);
  }, [pulse]);
  if (!pulse) return null;
  return <PulsePlane3D pulse={pulse} y={pulse.at_mm + 0.3} frame={Math.min(f, pulse.frames.length - 1)} fadeOut={f >= pulse.frames.length ? 1 - (f - pulse.frames.length) / 14 : 1} />;
}

/** The hero's real example: the nRF52840 sample board with its solver field and pulse (public/fixtures/nrf-sim). Null until loaded or when the files are absent. */
function useNrfHero() {
  const [d, setD] = useState<{ board: Board; fields: Fields | null; pulse: PulseData | null; f_res: number | null } | null | undefined>(undefined);
  const [pulse, setPulse] = useState<PulseData | null>(null);
  useEffect(() => {
    let live = true;
    (async () => {
      const [src, board] = await Promise.all([nrfSampleSource(), fetch(NRF_BOARD.board).then((r) => r.json())]);
      const job = src.jobs.find((j) => j.kind === "confirm");
      if (!job) throw new Error("no confirm run");
      const fields = await loadField(src, job);
      if (live) setD({ board: normaliseBoard(board), fields, pulse: null, f_res: job.measurements.resonance ? job.measurements.resonance.value * 1e6 : null });
      // the pulse is 17 MB: it arrives after the field is already on screen
      const p = await loadPulse(src, job);
      if (live) setPulse(p);
    })().catch(() => { if (live) setD(null); });
    return () => { live = false; };
  }, []);
  return { hero: d, pulse };
}
const none = { l: 0, r: 0, t: 0, b: 0 };
const noop = () => {};

const STEPS = [
  ["upload", "Upload", "Drop a KiCad project. TrustPCB reads the real geometry of every track, pad and part."],
  ["gauge", "Baseline", "KiCad rule checks and code analyzers measure the board, with no AI involved."],
  ["search", "Investigate", "An agent forms hypotheses and runs real electromagnetic simulations to test them."],
  ["flag", "Mark up", "Every issue is pinned to a real object on the board, with the numbers that prove it."],
];

/** The numbers of the bundled nRF52840 sample run (public/fixtures/nrf-sim), read from its own files. Null when they are absent: the strip then stays qualitative. */
interface StoryNumbers { base: number; asBuiltWorstDb: number; bestWorstDb: number; dipGhz: number; firstSeriesGhz: number | null; shortenedDipGhz: number | null; shortenedMm: number | null }
function useStoryNumbers(): StoryNumbers | null {
  const [n, setN] = useState<StoryNumbers | null>(null);
  useEffect(() => {
    let live = true;
    const j = (u: string) => fetch(`/fixtures/nrf-sim/${u}`).then((r) => (r.ok ? r.json() : Promise.reject(new Error(u))));
    Promise.all([j("summary.json"), j("tune_matching.json"), j("whatif_shorten_6mm_screen/summary.json").catch(() => null), j("whatif_shorten_6mm_screen/meta.json").catch(() => null)]).then(([s, t, w, m]) => {
      const base = s?.s11?.as_built?.band_worst_db, best = t?.best?.result?.band_worst_db, dip = s?.sim?.f_res;
      if (![base, best, dip].every((v) => typeof v === "number")) return;
      const op = (m?.scenario?.whatif_ops ?? []).find((o: { op: string }) => o.op === "adjust_antenna_length");
      if (live) setN({ base: acceptedPct(base)!, asBuiltWorstDb: base, bestWorstDb: best, dipGhz: dip / 1e9, firstSeriesGhz: s.sim.first_series_resonance_hz ? s.sim.first_series_resonance_hz / 1e9 : null, shortenedDipGhz: w?.sim?.f_res ? w.sim.f_res / 1e9 : null, shortenedMm: op ? Math.abs(op.delta_mm) : null });
    }).catch(() => {});
    return () => { live = false; };
  }, []);
  return n;
}

const HOW = [
  ["gauge", "Measure", "Code measures the board first: shorts, clearances, return paths, the length and shape of the antenna feed."],
  ["search", "Suspect", "The agent lists what could explain a problem, for example the antenna length, the matching parts or nearby copper."],
  ["layers", "Screen", "A quick solver run per suspect, for comparing only. It rules suspects out and points at the likely cause."],
  ["check", "Confirm", "The best candidate is run again at higher quality. Only the confirmed numbers are quoted, and the report says when the screen was too optimistic."],
];

function HowItWorks() {
  const n = useStoryNumbers();
  const best = n ? acceptedPct(n.bestWorstDb) : null;
  return (
    <section className="ld-sec">
      <div className="ld-sec-head"><span className="idx">How an investigation works</span><h2 className="heading">Suspects, then evidence.</h2></div>
      <ol className="ld-steps">
        {HOW.map(([ic, t, d]) => (<li key={t}><span className="ld-step-ic"><Icon name={ic} size={18} /></span><h3>{t}</h3><p>{d}</p></li>))}
      </ol>
      {n && best != null && (
        <div className="ld-story">
          <div className="ld-story-copy">
            <span className="idx">Example: the bundled nRF52840 sample run, not your board</span>
            <p>
              As drawn, this antenna&apos;s deepest dip sits at <b className="num">{n.dipGhz.toFixed(2)} GHz</b>{n.firstSeriesGhz ? <> and its first series resonance at <b className="num">{n.firstSeriesGhz.toFixed(2)} GHz</b></> : null}, far below the 2.4 GHz band, so the worst S11 in the band is <b className="num">{n.asBuiltWorstDb.toFixed(1)} dB</b>.
              {n.shortenedDipGhz && n.shortenedMm ? <> A screen run with the antenna {n.shortenedMm} mm shorter moves the dip to <b className="num">{n.shortenedDipGhz.toFixed(2)} GHz</b>, the direction the length says it should.</> : null}
              {" "}Re-tuning the matching parts alone reaches <b className="num">{n.bestWorstDb.toFixed(1)} dB</b> worst in the band: better, but not the <span className="num">-10 dB</span> target. That is reported as improved, target not met, with the parts to try.
            </p>
          </div>
          <PowerPair size="md" note={false}
            before={{ value: n.base, label: "As drawn", sub: "worst point of the band" }}
            after={{ value: best, label: "Matching parts re-tuned", sub: "worst point of the band, calculated on the solver result" }} />
        </div>
      )}
    </section>
  );
}

export function Landing() {
  const { demo } = useAppEnv();
  const calm = useReducedMotion();
  const [rf, setRf] = useState<RfBundle | null>(null);
  const nrf = useNrfHero();
  const useRealBoard = !!nrf.hero?.fields?.xy;
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [fx, setFx] = useState<{ curve: Curve; curve2: Curve; band: { lo_ghz: number; hi_ghz: number; target_db: number } } | null>(null);
  useEffect(() => { listProjects().then(setProjects).catch(() => setProjects([])); }, []);
  // the patch run is only the fallback when the nRF sample files are missing
  useEffect(() => { if (nrf.hero === null) loadPatchSim().then(setRf); }, [nrf.hero]);
  // illustrative curves exist only in fixture mode
  useEffect(() => { if (process.env.NEXT_PUBLIC_TRUSTPCB_FIXTURES === "1") void loadFixtures().then((m) => setFx({ curve: m.resolveCurve("base")!, curve2: m.resolveCurve("confirm")!, band: m.TARGET_BAND })); }, []);
  const latest = projects.find((p) => p.latest_review_id);
  const sampleHref = FIXTURE_MODE ? "/reviews/r-0042" : latest ? `/reviews/${latest.latest_review_id}` : null;
  const board: Board | null = useMemo(() => (rf ? scenarioBoard(rf) : null), [rf]);
  const empty = useMemo(() => ({ marks: [] as never[], meta: {} }), []);

  return (
    <main id="main" tabIndex={-1} className="ld">
      <section className="ld-hero">
        <div className="ld-scene" aria-hidden>
          {useRealBoard && nrf.hero ? (
            <Scene3D
              board={nrf.hero.board} glbUrl={NRF_BOARD.glb} marks={empty.marks} meta={empty.meta} selectedId={null} onSelect={noop}
              explode={0} insets={none} hero autoRotate={!calm} showMarks={false} boardDims={false} shift={[0.22, 0.05]} view3d={{ az: 0.62, el: 0.6, scale: 1.55 }}
            >
              {nrf.hero.fields?.xy && <WaveField3D slice={nrf.hero.fields.xy} />}
              {nrf.hero.fields?.xz && <WaveField3D slice={nrf.hero.fields.xz} opacity={0.75} />}
              <PulseLoop pulse={nrf.pulse} />
            </Scene3D>
          ) : nrf.hero === null && board && rf && (
            <Scene3D
              bare center={[0, 0.8, 0]} board={board} glbUrl="" marks={empty.marks} meta={empty.meta} selectedId={null} onSelect={noop}
              explode={0} insets={none} hero autoRotate={!calm} showMarks={false} shift={[0.2, 0.02]}
            >
              <PatchBoard3D rf={rf} />
              {rf.fieldXY && <WaveField3D slice={rf.fieldXY} />}
              {rf.fieldXZ && <WaveField3D slice={rf.fieldXZ} />}
              <PulseLoop pulse={rf.pulse} />
            </Scene3D>
          )}
        </div>
        <header className="ld-nav">
          <Logo />
          <nav>
            <Link href="/projects"><Icon name="folder" size={16} /><span>Projects</span></Link>
            <Link href="/compare"><Icon name="compare" size={16} /><span>Compare versions</span></Link>
            {sampleHref && <Link href={sampleHref}><Icon name="board" size={16} /><span>{FIXTURE_MODE ? "Sample review" : "Latest review"}</span></Link>}
          </nav>
          <span style={{ flex: 1 }} />
          <FixtureBadge />
          <DemoNotice />
          {!demo && <Link className="btn sm" href="/upload"><Icon name="upload" size={14} /> Upload a board</Link>}
        </header>
        <div className="ld-copy">
          <span className="idx rise" style={{ animationDelay: "100ms" }}>Open-source inspection layer for circuit boards</span>
          <h1 className="heading">
            <span className="rise l1" style={{ animationDelay: "200ms" }}>Others read your datasheets.</span>
            <span className="rise l2" style={{ animationDelay: "380ms" }}>TrustPCB tests your board.</span>
          </h1>
          <p className="rise" style={{ animationDelay: "560ms" }}>
            Upload a KiCad board. Get it marked up like a senior engineer went over the drawing: layout measurements, real simulations, and tested fixes. Nothing is edited. Nothing is guessed.
          </p>
          <div className="ld-cta rise" style={{ animationDelay: "720ms" }}>
            {!demo && <Link className="btn primary big" href="/upload"><Icon name="upload" size={15} /> Upload a board</Link>}
            {sampleHref && <Link className="btn big" href={sampleHref}><Icon name="board" size={16} /> {FIXTURE_MODE ? "Watch a sample review" : "Open your latest review"}</Link>}
          </div>
          <div className="ld-labels rise" style={{ animationDelay: "880ms" }}>
            <span className="idx">Every claim is labelled</span>
            <ClassStamp cls="measured" /><ClassStamp cls="simulated" /><ClassStamp cls="opinion" /><ClassStamp cls="not_tested" />
          </div>
        </div>
        <div className="ld-fig rise" style={{ animationDelay: "1000ms" }}>
          <span>{useRealBoard
            ? `nRF52840 node, 66 x 53 mm. Field at 2.44 GHz and the launch pulse from a real solver run, time slowed to be visible. As drawn, this antenna's deepest dip sits at ${nrf.hero?.f_res ? (nrf.hero.f_res / 1e9).toFixed(2) : "1.87"} GHz, off the band: the kind of thing TrustPCB reports.`
            : "2.5 GHz patch antenna, 60 x 50 mm. Complex field from the solver, phase locked, time slowed to be visible."}</span>
          <span className="stamp sim"><i />Real openEMS output</span>
        </div>
      </section>

      <section className="ld-sec">
        <div className="ld-sec-head"><span className="idx">Method</span><h2 className="heading">An inspection, not a rewrite.</h2></div>
        <ol className="ld-steps">
          {STEPS.map(([ic, t, d]) => (
            <li key={t}>
              <span className="ld-step-ic"><Icon name={ic} size={18} /></span>
              <h3>{t}</h3><p>{d}</p>
            </li>
          ))}
        </ol>
      </section>

      <HowItWorks />

      {projects.length > 0 && <section className="ld-sec">
        <div className="ld-sec-head"><span className="idx">{FIXTURE_MODE ? "Sample reviews" : "Your boards"}</span><h2 className="heading">{FIXTURE_MODE ? "See finished reviews." : "Pick up where you left off."}</h2></div>
        <div className="ld-cards">
          {projects.slice(0, 3).map((p, i) => (
            <Link key={p.id} href={p.latest_review_id ? `/reviews/${p.latest_review_id}` : `/projects/${p.id}`} className="ld-card">
              <div className="ld-card-top"><span className="idx">Review {p.latest_review_id.slice(0, 6)}</span><span className="idx">{p.latest_version}</span></div>
              <h3>{p.name}</h3>
              <p className="dim">{p.kind}</p>
              <div className="ld-card-graph">
                {fx && p.rf !== "n/a" && <TuningGraph curves={i === 1 ? [{ id: "a", label: "a", ...fx.curve2 }] : [{ id: "a", label: "a", ...fx.curve }, { id: "b", label: "b", ...fx.curve2 }]} band={fx.band} height={64} compact />}
                {!fx && <div className="dim" style={{ fontSize: 12.5, padding: "20px 0" }}>{p.rf === "n/a" ? "No radio part detected." : "Open the review for its RF result."}</div>}
              </div>
              <div className="ld-card-foot"><SevBar counts={p.counts} size="sm" /><span className="ld-card-open">Open review <Icon name="arrow" size={13} /></span></div>
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
