"use client";
import "./rf.css";
import Link from "next/link";
import { Fragment, useEffect, useMemo, useState } from "react";
import type { BoardBundle } from "../../lib/api";
import {
  acceptedForJob, acceptedOf, describeFreq, describeOps, dipsOf, fixedDipsMhz, fmtPct, isDone, isFixedDip, isScreen, loadCurrent, loadCurves, loadField, loadMeta, loadPattern, loadPulse, loadSummary, measure,
  QUALITY_NOTE, zFromGamma, type Curves, type SimJob, type SimSource,
} from "../../lib/sims";
import type { Headline, VersionChange as VC } from "../../lib/story.mjs";
import { VersionChange } from "./VersionChange";
import { useAsync } from "../../lib/use-async";
import { cssGradient, FIELD_STOPS, SIGNED_STOPS } from "../../lib/ramp.mjs";
import { reducedMotion, useNarrow, useReducedMotion } from "../../lib/hooks";
import { Icon, Panel } from "../ui";
import { SimStage, type SimView } from "./SimStage";
import { SmithChart, TuningGraph, type CurveSpec } from "./charts";
import { PartialWin } from "./PartialWin";
import { ACCEPTED_LABEL, PowerBar } from "./PowerPair";

export interface Band { lo_ghz: number; hi_ghz: number; target_db: number }
type Trace = "antenna" | "both" | "as_built";

const VIEWS: { key: SimView; label: string; icon: string; needs: string[]; why: string }[] = [
  { key: "wave", label: "Field", icon: "wave", needs: ["field.json", "field_xy.bin"], why: "field" },
  { key: "pulse", label: "Pulse", icon: "bolt", needs: ["pulse.json", "pulse.bin"], why: "pulse" },
  { key: "pattern", label: "Pattern", icon: "cube", needs: ["pattern.json"], why: "radiation pattern" },
  { key: "current", label: "Current", icon: "layers", needs: ["current.json", "current.bin"], why: "surface current" },
];
const canShow = (job: SimJob | null, v: SimView) => !!job && VIEWS.find((x) => x.key === v)!.needs.every((f) => job.files.includes(f));
const OVERLAY_COLORS = ["var(--paper-3)", "var(--sev-medium)", "var(--paper-4)"];
const ghz = (hz: number) => (hz / 1e9).toFixed(3);
const kindLabel = (k: SimJob["kind"]) => (k === "base" ? "Base" : k === "confirm" ? "Confirm" : "What-if");

function JobCard({ job, active, overlaid, onActive, onOverlay }: { job: SimJob; active: boolean; overlaid: boolean; onActive: () => void; onOverlay: () => void }) {
  const ops = describeOps(job.whatif_ops, job.material);
  const done = isDone(job);
  const dip = measure(job, "s11_dip_1"), depth = measure(job, "s11_dip_1_depth") ?? measure(job, "s11_min");
  const res = measure(job, "resonance");
  const f = dip ?? res;
  const acc = acceptedOf(job.measurements).headline?.worst ?? null;
  return (
    <div className={`rf-job ${active ? "on" : ""} ${done ? "" : "wait"}`}>
      <button className="rf-job-main" onClick={onActive} disabled={!done} aria-pressed={active} title={done ? "Show this run" : `This run is ${job.status}`}>
        <span className="rf-job-top">
          {job.ref && <b className="mono">{job.ref}</b>}
          <span className="idx">{kindLabel(job.kind)}</span>
          {job.suspect && <span className="idx">{job.suspect}</span>}
          <span style={{ flex: 1 }} />
          {isScreen(job) ? <span className="stamp" title={QUALITY_NOTE}>Screen, compare only</span> : job.quality === "confirm" ? <span className="stamp solid">Confirm</span> : null}
        </span>
        <span className="rf-job-name">{job.label ?? "Simulation"}</span>
        {ops && <span className="rf-job-ops mono">{ops}</span>}
        <span className="rf-job-num mono">
          {done && f != null && depth != null ? <>dip <b>{(f / 1000).toFixed(3)}</b> GHz <b>{depth.toFixed(1)}</b> dB</> : done ? "no numbers recorded" : job.status}
          {done && acc != null && <span className="rf-acc" title={ACCEPTED_LABEL}><b>{fmtPct(acc)}</b> % accepted</span>}
          {job.trust && <span className={`stamp ${job.trust.status === "ok" ? "" : "sig"}`} title={job.trust.reasons.join("; ") || undefined}>Trust {job.trust.status === "ok" ? "ok" : "failed"}</span>}
        </span>
        {!done && job.status !== "failed" && job.status !== "canceled" && job.status !== "timeout" && job.status !== "interrupted" && (
          <span className="meter sig"><b style={{ width: `${(job.progress ?? 0) * 100}%` }} /></span>
        )}
      </button>
      {done && <button className={`rf-job-ov ${overlaid ? "on" : ""}`} onClick={onOverlay} aria-pressed={overlaid} disabled={active} title={active ? "Shown" : overlaid ? "Remove from the tuning graph" : "Overlay on the tuning graph"}><Icon name={overlaid || active ? "check" : "plus"} size={12} /> {active ? "Shown on the board" : overlaid ? "On the tuning graph" : "Add to the tuning graph"}</button>}
    </div>
  );
}

export function RfLabView({ reviewId, source, bundle, title, goalBand, notice, sample = false, initialJob = null, headline = null, versionChange = null }: {
  reviewId: string; source: SimSource; bundle: BoardBundle | null; title: string; goalBand: Band | null; notice?: string; sample?: boolean; initialJob?: string | null; headline?: Headline | null; versionChange?: VC | null;
}) {
  const jobs = source.jobs;
  const doneJobs = useMemo(() => jobs.filter(isDone), [jobs]);
  // the run shown first: the confirm run of the board as drawn (a confirm of a changed antenna is a refine attempt, not the board), else any confirm with a field, else the latest
  const preferred = useMemo(() => {
    const latest = [...doneJobs].reverse();
    const asDrawn = (j: SimJob) => !(j.whatif_ops ?? []).length && !j.material;
    const confirmWithField = latest.filter((j) => j.kind === "confirm" && j.files.includes("field.json"));
    return confirmWithField.find(asDrawn) ?? confirmWithField[0] ?? latest.find((j) => j.files.includes("field.json")) ?? doneJobs[doneJobs.length - 1] ?? null;
  }, [doneJobs]);
  const [activeId, setActiveId] = useState<string | null>(initialJob);
  const [overlay, setOverlay] = useState<Record<string, boolean>>({});
  const active = jobs.find((j) => j.job_id === activeId) ?? preferred;
  useEffect(() => { if (!activeId && preferred) setActiveId(preferred.job_id); }, [activeId, preferred]);
  // the base run is the default reference to overlay under a later run, unless the viewer chose otherwise
  const baseRef = useMemo(() => doneJobs.find((j) => j.kind === "base" && j.job_id !== active?.job_id) ?? null, [doneJobs, active]);
  const overlaid = useMemo(() => doneJobs.filter((j) => j.job_id !== active?.job_id && (overlay[j.job_id] ?? j.job_id === baseRef?.job_id)), [doneJobs, active, overlay, baseRef]);

  const [view, setView] = useState<SimView>("wave");
  const [mode, setMode] = useState<"2d" | "3d">("3d");
  const [trace, setTrace] = useState<Trace>("both");
  const [marker, setMarker] = useState<number | null>(null);
  const [paused, setPaused] = useState(false);
  const [frame, setFrame] = useState(0);
  const [pulsePlay, setPulsePlay] = useState(!reducedMotion());
  const calm = useReducedMotion();
  const narrow = useNarrow();

  const k = active?.job_id ?? "";
  const summary = useAsync(active ? () => loadSummary(source, active) : null, `s:${source.id}:${k}`).data ?? null;
  const meta = useAsync(active ? () => loadMeta(source, active) : null, `m:${source.id}:${k}`).data ?? null;
  const curves = useAsync<Curves>(active ? () => loadCurves(source, active) : null, `c:${source.id}:${k}`).data ?? null;
  const overlayKey = overlaid.map((j) => j.job_id).join(",");
  const overlayCurves = useAsync(overlaid.length ? () => Promise.all(overlaid.map(async (j) => ({ job: j, c: await loadCurves(source, j) }))) : null, `o:${source.id}:${overlayKey}`).data ?? null;

  // pick a view this run can show
  useEffect(() => { if (active && !canShow(active, view)) { const v = VIEWS.find((x) => canShow(active, x.key)); if (v) setView(v.key); } }, [active, view]);
  const needField = view === "wave" && canShow(active, "wave");
  const fields = useAsync(active && needField ? () => loadField(source, active) : null, `f:${source.id}:${k}:${needField}`);
  const needPulse = view === "pulse" && canShow(active, "pulse");
  const pulse = useAsync(active && needPulse ? () => loadPulse(source, active) : null, `p:${source.id}:${k}:${needPulse}`);
  const needCur = view === "current" && canShow(active, "current");
  const current = useAsync(active && needCur ? () => loadCurrent(source, active) : null, `u:${source.id}:${k}:${needCur}`);
  const needPat = view === "pattern" && canShow(active, "pattern");
  const pattern = useAsync(active && needPat ? () => loadPattern(source, active) : null, `t:${source.id}:${k}:${needPat}`);

  useEffect(() => {
    if (view !== "pulse" || !pulse.data || !pulsePlay) return;
    const n = pulse.data.frames.length;
    const id = setInterval(() => setFrame((f) => { if (f >= n - 1) { setPulsePlay(false); return n - 1; } return f + 1; }), 90);
    return () => clearInterval(id);
  }, [view, pulse.data, pulsePlay]);

  const bandArr = summary?.setup?.band_ghz ?? meta?.setup?.band_ghz ?? (goalBand ? [goalBand.lo_ghz, goalBand.hi_ghz] as [number, number] : null);
  const band: Band | null = bandArr ? { lo_ghz: bandArr[0], hi_ghz: bandArr[1], target_db: goalBand?.target_db ?? -10 } : null;
  const bandHz: [number, number] | null = band ? [band.lo_ghz, band.hi_ghz] : null;

  const antenna = curves?.antenna ?? null, asBuilt = curves?.asBuilt ?? null;
  const specs: CurveSpec[] = useMemo(() => {
    const out: CurveSpec[] = [];
    (overlayCurves ?? []).forEach(({ job, c }, i) => {
      if (c.antenna) out.push({ id: `o-${job.job_id}`, label: `${job.ref ?? ""} ${job.label ?? kindLabel(job.kind)}`.trim(), f_ghz: c.antenna.f_ghz, s11_db: c.antenna.s11_db, color: OVERLAY_COLORS[i % OVERLAY_COLORS.length], dashed: true, width: 1.3, opacity: 0.9 });
    });
    if (asBuilt && (trace === "both" || trace === "as_built")) out.push({ id: "asbuilt", label: "As built (board's matching parts)", f_ghz: asBuilt.f_ghz, s11_db: asBuilt.s11_db, color: "var(--sim-4)", dashed: trace === "both", width: trace === "both" ? 1.4 : 1.9 });
    if (antenna && (trace === "both" || trace === "antenna" || !asBuilt)) out.push({ id: "antenna", label: `${active?.ref ?? ""} antenna only`.trim(), f_ghz: antenna.f_ghz, s11_db: antenna.s11_db, color: "var(--paper)", dashed: false, width: 1.9 });
    return out;
  }, [overlayCurves, antenna, asBuilt, trace, active]);
  const mainCurve = specs[specs.length - 1] ?? null;

  const { dips: rawDips, fromSummary } = useMemo(() => dipsOf(trace === "as_built" ? null : summary, trace === "as_built" ? asBuilt : antenna), [summary, antenna, asBuilt, trace]);
  // dips that stayed put when the geometry changed are not the antenna's resonance (a `fixed_dip_N` measurement of any run of this review)
  const fixedMhz = useMemo(() => fixedDipsMhz(jobs), [jobs]);
  const dips = useMemo(() => rawDips.map((d) => ({ ...d, fixed: isFixedDip(d.f_ghz, fixedMhz) })), [rawDips, fixedMhz]);
  const accepted = useMemo(() => acceptedForJob(active, curves, bandHz), [active, curves, bandHz]);
  const sumBand = {
    asBuilt: summary?.s11?.as_built?.band_worst_db ?? (active ? measure(active, "as_built_band_worst_s11") : null),
    antenna: summary?.s11?.antenna_only?.band_worst_db ?? (active ? measure(active, "antenna_only_band_worst_s11") : null),
  };
  const fsr = summary?.sim?.first_series_resonance_hz;
  useEffect(() => { setMarker(dips[0]?.f_ghz ?? null); }, [k, dips.length]); // eslint-disable-line react-hooks/exhaustive-deps

  const smithMain = curves?.zin ?? null;
  const smithAsBuilt = useMemo(() => (asBuilt?.re && asBuilt.im ? { f_ghz: asBuilt.f_ghz, ...zFromGamma(asBuilt.re, asBuilt.im, curves?.zin ? 50 : 50) } : null), [asBuilt, curves]);

  const insets = narrow ? { l: 0, r: 0, t: 0, b: 0 } : { l: 358, r: 450, t: 66, b: 74 };
  const screen = isScreen(active);
  const sim = summary?.sim;
  const freqNote = (() => {
    if (view === "wave" && fields.data) {
      const off = sim?.f_res && Math.abs(fields.data.f_hz - sim.f_res) / fields.data.f_hz > 0.02;
      return `Field at ${describeFreq(fields.data.f_hz, bandHz)}, relative units.${off ? ` The deepest S11 dip is elsewhere (${ghz(sim!.f_res!)} GHz); this shows how the antenna behaves at the target.` : ""}`;
    }
    if (view === "pattern" && pattern.data) {
      const atDip = sim?.f_res && Math.abs(pattern.data.f_hz - sim.f_res) / pattern.data.f_hz < 0.005;
      return `Pattern at ${describeFreq(pattern.data.f_hz, bandHz)}${atDip ? ", the deepest S11 dip" : ""}. Field and current are drawn at the band frequency, so the two can differ.`;
    }
    if (view === "current" && current.data) return `Current at ${describeFreq(current.data.f_hz, bandHz)}.`;
    if (view === "pulse") return "A broadband pulse leaving the feed: it shows how energy spreads, not one frequency.";
    return "";
  })();
  const loading = (view === "wave" && fields.data === undefined) || (view === "pulse" && pulse.data === undefined && needPulse) || (view === "current" && current.data === undefined && needCur) || (view === "pattern" && pattern.data === undefined && needPat);
  const unavailable = active && !canShow(active, view);

  const stage = (
    <div className="rf-stage">
      {bundle && active ? (
        <SimStage
          board={bundle.board} glbUrl={bundle.glbUrl} sceneInfo={bundle.scene} view={view} mode={mode} meta={meta}
          fields={fields.data} current={current.data} pulse={pulse.data} pattern={pattern.data} frame={frame} paused={paused} calm={calm} insets={insets}
        />
      ) : <div className="rf-loading idx">{bundle ? "No finished run yet" : "Loading the board"}</div>}
      {loading && <div className="rf-loading idx">Loading the {VIEWS.find((v) => v.key === view)!.why} data</div>}
      {unavailable && <div className="rf-loading idx">This run kept no {VIEWS.find((v) => v.key === view)!.why} files</div>}
    </div>
  );

  const runs = (
    <Panel className="rf-runs" title="Simulations" bodyClass="rf-body tight" actions={<span className="idx">{doneJobs.length} of {jobs.length} done</span>}>
      <div className="rf-joblist">
        {jobs.map((j) => (
          <JobCard key={j.job_id} job={j} active={j.job_id === active?.job_id} overlaid={overlaid.some((o) => o.job_id === j.job_id)} onActive={() => { setActiveId(j.job_id); setFrame(0); }}
            onOverlay={() => setOverlay((o) => ({ ...o, [j.job_id]: !(overlaid.some((x) => x.job_id === j.job_id)) }))} />
        ))}
      </div>
    </Panel>
  );

  const ramp = view === "wave" ? SIGNED_STOPS : FIELD_STOPS;
  const controls = (
    <Panel className="rf-ctl" title="On the board" bodyClass="rf-body">
      <div className="seg rf-views" role="tablist" aria-label="Simulation view">
        {VIEWS.map((v) => (
          <button key={v.key} role="tab" aria-selected={view === v.key} disabled={!canShow(active, v.key)} title={canShow(active, v.key) ? undefined : `This run kept no ${v.why} files`}
            onClick={() => { setView(v.key); if (v.key === "pulse") { setFrame(0); setPulsePlay(true); } }}><Icon name={v.icon} size={13} /> {v.label}</button>
        ))}
      </div>
      <div className="rf-legend">
        <span className="idx">{view === "wave" ? "E-field, instantaneous, Ez" : view === "pulse" ? "Pulse, |E|" : view === "pattern" ? "Directivity" : "Surface current, |Js|"}</span>
        <div className="rf-ramp" style={{ background: cssGradient(ramp) }} />
        <div className="rf-ramp-l mono">
          {view === "wave" ? <><span>negative</span><span>0</span><span>positive</span></> : view === "pattern" ? <><span>{pattern.data ? `${(pattern.data.dmax_dbi - 10).toFixed(0)} dBi` : ""}</span><span>{pattern.data ? `${pattern.data.dmax_dbi.toFixed(1)} dBi max` : ""}</span></> : <><span>0</span><span>{view === "current" ? "99.5th pct" : "peak"}</span></>}
        </div>
        <p className="dim">{freqNote || "Real openEMS output drawn on this version's board model."}</p>
        {view === "pattern" && pattern.data && <p className="dim">Peak {pattern.data.dmax_dbi.toFixed(1)} dBi at theta {pattern.data.dmax_theta_deg} deg, phi {pattern.data.dmax_phi_deg} deg (the bright dot). Radius covers the top 20 dB, colour the top 10 dB.</p>}
        {view === "current" && <p className="dim">Heat on the top copper. Sharp edges can exceed the scale.</p>}
      </div>
      <div className="rf-row">
        <div className="seg" role="group" aria-label="Board view">
          <button aria-pressed={mode === "3d" || view === "pattern"} onClick={() => setMode("3d")}><Icon name="cube" size={11} /> 3D</button>
          <button aria-pressed={mode === "2d" && view !== "pattern"} disabled={view === "pattern"} onClick={() => setMode("2d")}><Icon name="board" size={11} /> 2D</button>
        </div>
        <span className="idx rf-dashed" title="The dashed box is the region the solver modelled. The red ring is the port.">Dashed box: modelled region</span>
      </div>
    </Panel>
  );

  const numbers: [string, string][] = [];
  if (sim) {
    if (sim.f_res != null && sim.s11_min_db != null) numbers.push(["Deepest dip", `${ghz(sim.f_res)} GHz ${sim.s11_min_db.toFixed(1)} dB`]);
    if (sim.bandwidth) numbers.push(["-10 dB range", `${ghz(sim.bandwidth.f_low_hz)} to ${ghz(sim.bandwidth.f_high_hz)}`]);
  }
  if (summary?.s11?.antenna_only) numbers.push(["Band worst S11", `${summary.s11.antenna_only.band_worst_db.toFixed(1)} dB${summary.s11.as_built ? ` / ${summary.s11.as_built.band_worst_db.toFixed(1)} as built` : ""}`]);
  if (summary?.zin_at_band_centre) { const z = summary.zin_at_band_centre; numbers.push(["Zin at band", `${z.re.toFixed(0)} ${z.im < 0 ? "-" : "+"} j${Math.abs(z.im).toFixed(0)} ohm`]); }
  if (sim?.solve_s != null) numbers.push(["Solve time", `${Math.round(sim.solve_s)} s${sim.cells ? `, ${(sim.cells / 1e6).toFixed(2)} M cells` : ""}`]);
  if (sim?.dmax_dbi != null) numbers.push(["Peak directivity", `${sim.dmax_dbi.toFixed(1)} dBi${sim.efficiency_est != null ? `, eff. ~${(sim.efficiency_est * 100).toFixed(0)} %` : ""}${sim.pattern_f_hz != null ? ` at ${(sim.pattern_f_hz / 1e9).toFixed(2)} GHz` : ""}`]);
  const inBand = band && sim?.f_res ? sim.f_res / 1e9 >= band.lo_ghz && sim.f_res / 1e9 <= band.hi_ghz && (sim.s11_min_db ?? 0) <= band.target_db : null;

  const charts = (
    <Panel className="rf-charts" title="Tuning" bodyClass="rf-body" actions={<span className="rf-acts">
      {active?.trust && <span className={`stamp ${active.trust.status === "ok" ? "" : "sig"}`} title={active.trust.reasons.join("; ") || "Energy decay, passivity, mesh and port checks"}>{active.trust.status === "ok" ? "Trust ok" : "Trust failed"}</span>}
      {inBand != null && <span className={`stamp ${inBand ? "solid" : "sig"}`}>{inBand ? "Dip in band" : "Off target"}</span>}
      {screen ? <span className="stamp" title={QUALITY_NOTE}>Screen, compare only</span> : active?.quality === "confirm" ? <span className="stamp solid">Confirm</span> : null}
    </span>}>
      {versionChange && <VersionChange vc={versionChange} compact />}
      {headline && <PartialWin h={headline} compact />}
      {(accepted.asBuilt || accepted.antenna || sumBand.asBuilt != null || sumBand.antenna != null) && (
        <section className="rf-band" aria-label="In-band S11 and accepted power">
          <div className="rf-band-s11">
            <span className="idx">S11 in the band, worst point</span>
            <div className="rf-band-big mono">{sumBand.asBuilt != null ? <>{sumBand.asBuilt.toFixed(1).replace("-", "−")}<small> dB</small></> : sumBand.antenna != null ? <>{sumBand.antenna.toFixed(1).replace("-", "−")}<small> dB</small></> : "-"}</div>
            <span className="idx">{sumBand.asBuilt != null ? "As built" : "Antenna alone"}</span>
            {sumBand.asBuilt != null && sumBand.antenna != null && <span className="mono rf-band-small">{sumBand.antenna.toFixed(1).replace("-", "−")} dB antenna alone</span>}
            {band && <span className="mono rf-band-small">target {band.target_db} dB</span>}
          </div>
          <div className="rf-band-acc">
            {accepted.asBuilt && <PowerBar value={accepted.asBuilt.worst} label="Accepted power, as built" sub={accepted.asBuilt.mean != null ? `worst point, mean ${fmtPct(accepted.asBuilt.mean)} %` : "worst point"} size="md" />}
            {accepted.antenna && <PowerBar value={accepted.antenna.worst} label="Accepted power, antenna alone" sub={accepted.antenna.mean != null ? `worst point, mean ${fmtPct(accepted.antenna.mean)} %` : "worst point"} tone="before" size="sm" />}
          </div>
          <p className="idx rf-band-cap">{ACCEPTED_LABEL}{(accepted.asBuilt?.fromCurve || (!accepted.asBuilt && accepted.antenna?.fromCurve)) ? ". Worked out here from this run's own S11 curve." : ""}</p>
        </section>
      )}
      <div className="rf-row">
        <span className="idx">S11 and band</span><span style={{ flex: 1 }} />
        {asBuilt && (
          <div className="seg" role="group" aria-label="Which S11 trace">
            <button aria-pressed={trace === "antenna"} onClick={() => setTrace("antenna")}>Antenna</button>
            <button aria-pressed={trace === "both"} onClick={() => setTrace("both")}>Both</button>
            <button aria-pressed={trace === "as_built"} onClick={() => setTrace("as_built")}>As built</button>
          </div>
        )}
      </div>
      {specs.length ? (
        <TuningGraph curves={specs} band={band} marker={marker} onMarker={setMarker} height={214} dips={dips} vlines={fsr ? [{ f_ghz: fsr / 1e9, label: "1st series res. (length)" }] : undefined} />
      ) : <p className="dim">{curves === null ? "Loading the S11 curve." : "This run has no S11 curve."}</p>}
      <div className="rf-leg mono">
        {specs.slice().reverse().map((c) => (<span key={c.id}><i style={{ background: c.color, opacity: c.dashed ? 0.8 : 1 }} />{c.label}</span>))}
        {band && <span><i style={{ background: "var(--signal)" }} />Band {band.lo_ghz.toFixed(2)} to {band.hi_ghz.toFixed(2)} GHz, target {band.target_db} dB</span>}
      </div>
      <div className="rf-split">
        <div>
          <span className="idx">Smith chart, 50 ohm</span>
          {smithMain && band ? <SmithChart zin={smithMain} second={trace !== "antenna" ? smithAsBuilt : null} band={band} marker={marker} size={196} /> : <p className="dim">{band ? "No impedance file for this run." : "The target band is unknown."}</p>}
        </div>
        <div className="rf-dipcol">
          <span className="idx">S11 dips{fromSummary ? "" : ", found on the curve"}</span>
          {dips.length > 0 ? (
            <ol className="rf-dips mono" aria-label="S11 dips">
              {dips.map((d, i) => (
                <li key={i} className={d.fixed ? "fixed" : undefined}>
                  <b>{i + 1}</b><span>{d.f_ghz.toFixed(3)}</span><span>{d.db.toFixed(1)} dB</span>
                  {band && d.f_ghz >= band.lo_ghz && d.f_ghz <= band.hi_ghz && <span className="stamp solid">In band</span>}
                  {d.fixed && <span className="rf-fixed" title="This dip stayed at the same frequency when the antenna geometry changed, so it is not the antenna's own resonance. Rely on the in-band numbers and the first series resonance.">Fixed dip, unknown origin, not the antenna&apos;s resonance</span>}
                </li>
              ))}
            </ol>
          ) : <p className="dim" style={{ margin: 0, fontSize: 12 }}>No dip below -3 dB.</p>}
          {fsr ? <p className="dim rf-fsr mono"><i /><b>Length indicator:</b> 1st series resonance {ghz(fsr)} GHz, where the reactance crosses zero. It moves with the antenna length.</p> : null}
        </div>
      </div>
      {summary?.plausibility?.map((p, i) => (<p key={i} className="rf-warn">{p}</p>))}
      {screen && <p className="rf-warn quiet">{QUALITY_NOTE} A confirm run is the one to quote.</p>}
      <div className="rf-stats">
        {numbers.map(([a, b]) => (<div key={a}><span className="idx">{a}</span><b className="mono">{b}</b></div>))}
      </div>
      <p className="idx rf-note">Simulated. Not a substitute for a real antenna measurement.</p>
    </Panel>
  );

  const dock = (
    <div className="rf-dock">
      {view === "wave" && (<><button className="btn sm" disabled={calm} title={calm ? "Motion is off: your system asks for reduced motion." : undefined} onClick={() => setPaused(!paused)}><Icon name={paused ? "play" : "pause"} size={11} /> {paused ? "Play" : "Pause"}</button><span className="idx">Phase rotates at {fields.data ? ghz(fields.data.f_hz) : ""} GHz, time slowed</span></>)}
      {view === "pulse" && pulse.data && (<>
        <button className="btn sm" onClick={() => { setFrame(0); setPulsePlay(true); }}><Icon name="rotate" size={11} /> Play once</button>
        <input type="range" min={0} max={pulse.data.frames.length - 1} value={frame} onChange={(e) => { setPulsePlay(false); setFrame(+e.target.value); }} aria-label="Pulse frame" style={{ width: 220, accentColor: "var(--paper)" }} />
        <span className="idx">t = {((pulse.data.times_s[Math.min(frame, pulse.data.times_s.length - 1)] ?? 0) * 1e9).toFixed(2)} ns</span></>)}
      {view === "pattern" && <span className="idx">Drag to orbit. The balloon is centred on the antenna.</span>}
      {view === "current" && <span className="idx">Top copper, at {current.data ? ghz(current.data.f_hz) : ""} GHz</span>}
    </div>
  );

  return (
    <main className="rf">
      {stage}
      <header className="rf-top">
        <Link className="btn sm" href={sample ? "/" : `/reviews/${reviewId}`}><Icon name="back" size={13} /> {sample ? "Back to home" : "Back to review"}</Link>
        <div className="rf-top-id">
          <b className="rf-title">RF lab</b>
          <span className="rf-sub dim">{title}</span>
        </div>
        <span style={{ flex: 1 }} />
        <span className="stamp sim" title="Computed by the openEMS solver"><i />Simulated</span>
      </header>
      {notice && <div className="rf-banner mono"><span className="stamp fixture">Bundled sample</span> {notice}</div>}
      <div className="rf-left">{runs}{controls}</div>
      {charts}
      {dock}
    </main>
  );
}
