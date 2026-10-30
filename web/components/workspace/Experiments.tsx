"use client";
import Link from "next/link";
import { resolveCurve, TARGET_BAND } from "../../lib/api";
import type { ReviewState } from "../../lib/review-reducer.mjs";
import { TuningGraph, minOf } from "../rf/charts";
import { Icon } from "../ui";

/** Experiment strip: base, each test, confirmed. Updates live with sim events. */
export function ExperimentStrip({ state, reviewId }: { state: ReviewState; reviewId: string }) {
  const running = Object.values(state.sims).filter((s) => s.status !== "done");
  const exps = state.experiments;
  return (
    <div className="ex">
      <div className="ex-row">
        {exps.map((e, i) => {
          const c = resolveCurve(e.curve);
          const mn = c ? minOf(c) : null;
          const label = e.kind === "base" ? "BASE" : e.kind === "confirm" ? "CONFIRMED" : `TEST ${i}`;
          const ok = mn ? mn.db <= TARGET_BAND.target_db && mn.f >= TARGET_BAND.lo_ghz && mn.f <= TARGET_BAND.hi_ghz : false;
          return (
            <div key={e.id} className={`ex-cell rise ${e.kind}`}>
              <div className="ex-top"><span className="idx">{label}</span>{ok && <span className="stamp sig">In band</span>}</div>
              <div className="ex-graph">{c && <TuningGraph curves={[{ id: e.id, label: e.label, ...c }]} band={TARGET_BAND} height={46} compact />}</div>
              <div className="ex-name">{e.label}</div>
              <div className="mono ex-num">{e.summary ? <><b>{e.summary.f0_ghz.toFixed(3)}</b> GHz <b>{e.summary.s11_db.toFixed(1)}</b> dB</> : "-"}</div>
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
          <div className="ex-empty"><span className="idx">EXPERIMENTS</span><span className="dim">Each simulation the agent runs appears here with its tuning curve: base first, then every test, then the confirmed result.</span></div>
        )}
      </div>
      <Link className="btn sm ex-link" href={`/reviews/${reviewId}/rf`}><Icon name="wave" size={12} /> RF lab</Link>
    </div>
  );
}
