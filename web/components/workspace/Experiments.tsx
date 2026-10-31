"use client";
import Link from "next/link";
import { normaliseCurve } from "../../lib/curve";
import type { ReviewState } from "../../lib/review-reducer.mjs";
import { measurementLine } from "../../lib/review-view";
import { TuningGraph } from "../rf/charts";
import { Icon } from "../ui";

export interface Band { lo_ghz: number; hi_ghz: number; target_db: number }

/** Experiment strip: base, each test, confirmed. Updates live with sim events. */
export function ExperimentStrip({ state, reviewId, band, rfNote }: { state: ReviewState; reviewId: string; band: Band | null; rfNote: string | null }) {
  const running = Object.values(state.sims).filter((s) => s.status !== "done");
  const exps = state.experiments;
  return (
    <div className="ex">
      <div className="ex-row">
        {exps.map((e, i) => {
          const c = normaliseCurve(e.curve);
          const f0 = e.measurements?.f_res_hz?.value, s11 = e.measurements?.s11_min_db?.value;
          const label = e.kind === "base" ? "BASE" : e.kind === "confirm" ? "CONFIRMED" : `TEST ${i}`;
          const ok = band && f0 != null && s11 != null ? s11 <= band.target_db && f0 / 1e9 >= band.lo_ghz && f0 / 1e9 <= band.hi_ghz : null;
          return (
            <div key={e.id} className={`ex-cell rise ${e.kind}`}>
              <div className="ex-top"><span className="idx">{label}</span>{ok === true && <span className="stamp sig">In band</span>}{e.trust !== "ok" && <span className="stamp">Trust {e.trust}</span>}</div>
              <div className="ex-graph">{c && <TuningGraph curves={[{ id: e.id, label: e.label, ...c }]} band={band ?? undefined} height={46} compact />}</div>
              <div className="ex-name">{e.label}</div>
              <div className="mono ex-num">{e.measurements && Object.keys(e.measurements).length ? measurementLine(e.measurements).slice(0, 2).map((m) => (<span key={m.k}><b>{m.v}</b> {m.u} </span>)) : "-"}</div>
            </div>
          );
        })}
        {running.map((s) => (
          <div key={s.id} className="ex-cell run">
            <div className="ex-top"><span className="idx">{s.kind === "confirm" ? "CONFIRMING" : "RUNNING"}</span><span className="idx blink">{Math.round(s.pct * 100)}%</span></div>
            <div className="ex-prog"><div className="meter sig"><b style={{ width: `${s.pct * 100}%` }} /></div></div>
            <div className="ex-name">{s.label}</div>
            <div className="mono ex-num dim">{s.step}</div>
          </div>
        ))}
        {exps.length === 0 && running.length === 0 && (
          <div className="ex-empty">
            <span className="idx">EXPERIMENTS / NOT TESTED</span>
            <span className="dim">{rfNote ?? "No simulation has run in this review. Each one the agent runs appears here: base first, then every test, then the confirmed result."}</span>
          </div>
        )}
      </div>
      <Link className="btn sm ex-link" href={`/reviews/${reviewId}/rf`}><Icon name="wave" size={12} /> RF lab</Link>
    </div>
  );
}
