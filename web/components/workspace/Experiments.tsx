"use client";
import Link from "next/link";
import { useMemo } from "react";
import { simKindName, simStatusText } from "../../lib/board-sim-model.mjs";
import { fmtVal } from "../../lib/overlay-math.mjs";
import { normaliseCurve } from "../../lib/curve";
import type { ReviewState } from "../../lib/review-reducer.mjs";
import { measurementLine } from "../../lib/review-view";
import { loadCurves, type SimJob, type SimSource } from "../../lib/sims";
import { useAsync } from "../../lib/use-async";
import { TuningGraph } from "../rf/charts";
import "../simlab/progress.css";
import { Icon } from "../ui";

export interface Band { lo_ghz: number; hi_ghz: number; target_db: number }

/** The curve for a thumbnail: the one the sim_done event carried, else the run's own s11.json from the API. */
function Thumb({ curve, id, band, job, source }: { curve: unknown; id: string; band: Band | null; job: SimJob | null; source: SimSource | null }) {
  const own = normaliseCurve(curve);
  const fetched = useAsync(!own && job && source && job.files.includes("s11.json") ? () => loadCurves(source, job).then((c) => c.antenna) : null, `th:${job?.job_id}:${job?.files.length}`).data ?? null;
  const c = own ?? fetched;
  return <div className="ex-graph">{c ? <TuningGraph curves={[{ id, label: id, f_ghz: c.f_ghz, s11_db: c.s11_db }]} band={band ?? undefined} height={29} compact /> : <span className="idx dim">No curve</span>}</div>;
}

/** The experiments: base, each test, confirmed. Updates live with sim events: queued, progress, done with its curve. */
export function Experiments({ state, reviewId, band, rfNote, jobs = [], source = null }: {
  state: ReviewState; reviewId: string; band: Band | null; rfNote: string | null; jobs?: SimJob[]; source?: SimSource | null;
}) {
  const running = Object.values(state.sims).filter((s) => s.status !== "done");
  const exps = state.experiments;
  const byRef = useMemo(() => new Map(jobs.filter((j) => j.ref).map((j) => [j.ref!, j])), [jobs]);
  const suspectTitle = useMemo(() => {
    const m = new Map<string, string>();
    for (const inv of Object.values(state.investigations)) for (const s of inv.suspects) if (s.code) m.set(s.code, s.title ?? s.code);
    return m;
  }, [state.investigations]);
  const chip = (code?: string) => (code ? <span className="idx ex-sus" title={suspectTitle.get(code) ? `Suspect ${code}: ${suspectTitle.get(code)}` : `Suspect ${code}`}>{code}</span> : null);
  const kindChip = (k?: string | null) => (k ? <span className="idx ex-sus ex-kind" title={`${simKindName(k)} simulation of the board`}>{simKindName(k)}</span> : null);
  return (
    <div className="ex">
      <div className="ex-row">
        {exps.map((e, i) => {
          const job = byRef.get(e.id) ?? null;
          const f0 = e.measurements?.resonance?.value ?? e.measurements?.f_res_hz?.value, s11 = e.measurements?.s11_min?.value ?? e.measurements?.s11_min_db?.value;
          const f0ghz = f0 == null ? null : e.measurements?.resonance ? f0 / 1000 : f0 / 1e9;
          const label = e.kind === "base" ? "Base" : e.kind === "confirm" ? "Confirmed" : `Test ${exps.slice(0, i + 1).filter((x) => x.kind === "whatif").length}`;
          const ok = band && f0ghz != null && s11 != null ? s11 <= band.target_db && f0ghz >= band.lo_ghz && f0ghz <= band.hi_ghz : null;
          // the first series resonance is the length indicator; the deepest dip can jump between unrelated features of the curve
          const shift = e.delta?.series_res_hz as number | undefined;
          const board = !!e.simKind;                    // power, heat or signal: numbers, no tuning curve, and it opens in the simulation lab
          const body = (
            <>
              <div className="ex-top"><span className="idx">{label}</span>{kindChip(e.simKind)}{chip(e.suspect)}<span style={{ flex: 1 }} />{ok === true && <span className="stamp sig">In band</span>}{e.trust !== "ok" && <span className="stamp">Trust {e.trust}</span>}</div>
              {!board && <Thumb curve={e.curve} id={e.id} band={band} job={job} source={source} />}
              <div className="ex-name" title={e.label}>{e.id} {e.label}</div>
              <div className="ex-num">
                {shift != null
                  ? <span title={`Shift of the first series resonance against the base run, the indicator that moves with antenna length.${e.measurements ? ` ${measurementLine(e.measurements).slice(0, 2).map((m) => `${m.k} ${m.v} ${m.u}`).join(", ")}.` : ""}`}>1st series <b>{shift >= 0 ? "+" : "−"}{Math.abs(shift / 1e6).toFixed(0)}</b> MHz</span>
                  : board && e.measurements && Object.keys(e.measurements).length ? Object.entries(e.measurements).slice(0, 2).map(([k, m]) => (<span key={k} className="ex-meas"><span className="dim">{k.replace(/_/g, " ")}</span> <b>{fmtVal(m.value, m.unit)}</b></span>))
                  : e.measurements && Object.keys(e.measurements).length ? measurementLine(e.measurements).slice(0, 2).map((m) => (<span key={m.k}><b>{m.v}</b> {m.u} </span>)) : "-"}
              </div>
              {!board && e.quality === "screen" && <div className="ex-q" title="Screen runs are for comparing. Do not read them as absolute numbers.">Screen, compare only</div>}
            </>
          );
          return board ? (
            <Link key={e.id} href={`/reviews/${reviewId}/sim?kind=${e.simKind}&ref=${encodeURIComponent(e.id)}`} className={`ex-cell rise ${e.kind} link`} title="Open this run in the simulation lab">{body}</Link>
          ) : job ? (
            <Link key={e.id} href={`/reviews/${reviewId}/rf?job=${encodeURIComponent(job.job_id)}`} className={`ex-cell rise ${e.kind} link`} title="Open this run in the RF lab">{body}</Link>
          ) : (
            <div key={e.id} className={`ex-cell rise ${e.kind}`}>{body}</div>
          );
        })}
        {running.map((s) => (
          <div key={s.id} className="ex-cell run">
            <div className="ex-top"><span className="idx">{s.waiting ? "Waiting" : s.kind === "confirm" ? "Confirming" : "Running"}</span>{kindChip(s.simKind)}{chip(s.suspect)}<span style={{ flex: 1 }} />{!s.waiting && <span className="idx blink">{Math.round(s.pct * 100)}%</span>}</div>
            {!s.waiting && <div className="ex-prog"><div className="meter sig"><b style={{ width: `${s.pct * 100}%` }} /></div></div>}
            <div className="ex-name" title={s.label}>{s.id} {s.label}</div>
            <div className="ex-num dim">{simStatusText(s)}</div>
            {!s.simKind && s.quality === "screen" && <div className="ex-q">Screen, compare only</div>}
          </div>
        ))}
        {exps.length === 0 && running.length === 0 && (
          <div className="ex-empty">
            <span className="idx">Not tested</span>
            <span className="dim">{rfNote ?? "No simulation has run in this review. Each one the agent runs appears here: base first, then every test, then the confirmed result."}</span>
          </div>
        )}
      </div>
      <div className="ex-links">
        <Link className="btn ex-link" href={`/reviews/${reviewId}/rf`}><Icon name="wave" size={14} /> Open RF lab</Link>
        <Link className="btn ex-link" href={`/reviews/${reviewId}/sim`}><Icon name="flame" size={14} /> Open simulation lab</Link>
      </div>
    </div>
  );
}
