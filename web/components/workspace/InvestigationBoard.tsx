"use client";
import { useState } from "react";
import type { Investigation, ReviewState, Sim, Suspect } from "../../lib/review-reducer.mjs";
import { acceptedOf, fmtPct } from "../../lib/sims";
import { OUTCOME_TEXT, type StoryRound, type StoryTest } from "../../lib/story.mjs";

const VERDICT: Record<string, { text: string; cls: string }> = {
  ruled_out: { text: "Ruled out", cls: "ruled_out" },
  confirmed: { text: "Confirmed", cls: "confirmed" },
  inconclusive: { text: "Inconclusive", cls: "inconclusive" },
  not_tested: { text: "Not tested", cls: "not_tested" },
  open: { text: "Testing", cls: "testing" },
  testing: { text: "Testing", cls: "testing" },
  pending: { text: "Queued", cls: "pending" },
};
const db = (v: number | null | undefined) => (typeof v === "number" ? `${v.toFixed(1).replace("-", "−")} dB` : "-");
const bandWorst = (s: Test) => s.measurements?.as_built_band_worst_s11?.value ?? s.measurements?.s11_band_worst?.value ?? s.measurements?.antenna_only_band_worst_s11?.value ?? null;

/** One test the agent ran for a suspect, in a line: run, quality, worst S11 in the band, accepted power. Screen runs say they are for comparing only. */
type Test = { id: string; label: string; kind: string; quality?: string | null; suspect?: string | null; measurements?: Record<string, { value: number; unit: string }>; status: string };
function TestLine({ s }: { s: Test }) {
  const acc = s.measurements ? acceptedOf(s.measurements).headline?.worst ?? null : null;
  return (
    <li className="ib-test">
      <span className="mono ib-ref">{s.id}</span>
      <span className="ib-tl" title={s.label}>{s.label}</span>
      {s.quality === "screen" ? <span className="stamp" title="Screen runs are for comparing. Do not read them as absolute numbers.">Screen, compare only</span> : s.quality === "confirm" ? <span className="stamp solid">Confirm</span> : <span className="stamp" title={s.kind === "matching" ? "Matching parts calculated on a stored result: no solver time" : undefined}>{s.kind === "matching" ? "Matching, no solver" : s.kind}</span>}
      <span className="mono ib-tn">{s.status === "done" ? <>{db(bandWorst(s))}{acc != null && <> / {fmtPct(acc)} %</>}</> : s.status}</span>
    </li>
  );
}

function SuspectCard({ s, sims }: { s: Suspect; sims: Test[] }) {
  const v = VERDICT[s.status] ?? { text: s.status.replace(/_/g, " "), cls: s.status };
  const mine = sims.filter((x) => x.suspect && x.suspect === s.code);
  return (
    <li className={`ib-sus ${v.cls}`}>
      <div className="ib-sus-top">
        <span className="idx">{s.code}</span>
        <span className="ib-round idx">{s.round ? `REFINE ROUND ${s.round}` : "FIRST PASS"}</span>
        <span className={`stamp ib-verdict ${v.cls}`}>{v.text}</span>
      </div>
      <p className="ib-title">{s.title}</p>
      <div className="ib-what">
        <span className="idx">WHAT WAS TESTED</span>
        {mine.length ? <ul className="ib-tests">{mine.map((m) => (<TestLine key={m.id} s={m} />))}</ul> : <span className="dim ib-none">{s.status === "not_tested" ? "No run: the investigation closed before this was tested." : "No simulation run yet."}</span>}
      </div>
      {s.conclusion && <p className="ib-why"><span className="idx">WHY</span> {s.conclusion}</p>}
    </li>
  );
}

/** "Screen predicted X. Confirm run gave Y." for each confirmation round (both are simulations). */
function ScreenConfirm({ rounds }: { rounds: StoryRound[] }) {
  return (
    <div className="ib-story">
      {rounds.map((r) => (
        <div key={`${r.investigation}-${r.round}`} className="ib-story-r">
          <span className="idx">{r.round ? `REFINE ROUND ${r.round}` : "FIRST CONFIRMATION"}: SCREEN AGAINST CONFIRM</span>
          {r.rows.map((x) => {
            const over = x.screen != null && x.confirm != null && x.screen < x.confirm - 0.5;
            return (
              <p key={x.label} className="ib-pc">
                <b>{x.label}.</b> Screen predicted <span className="mono">{db(x.screen)}</span> in the band{x.screenRef ? <span className="idx"> {x.screenRef}</span> : null}. Confirm run gave <span className="mono ib-meas">{db(x.confirm)}</span>{x.confirmRef ? <span className="idx"> {x.confirmRef}</span> : null}.
                {over && <span className="dim"> The screen was too optimistic by {(x.confirm! - x.screen!).toFixed(1)} dB.</span>}
              </p>
            );
          })}
          {r.outcome && <p className="ib-out"><span className="stamp">{r.outcome.replace(/_/g, " ")}</span> {r.statement ?? OUTCOME_TEXT[r.outcome] ?? ""}</p>}
        </div>
      ))}
      <p className="idx ib-compare">SCREEN RUNS ARE FOR COMPARING. THE CONFIRM NUMBERS ARE THE RESULT.</p>
    </div>
  );
}

/** The investigation as a board: each suspect with what was tested and its verdict, then what screen predicted against what the confirm run gave, then what was checked and cleared. */
export function InvestigationBoard({ state, story, tests = [] }: { state: ReviewState; story: StoryRound[]; tests?: StoryTest[] }) {
  const [showCleared, setShowCleared] = useState(false);
  const live = Object.values(state.sims) as (Sim & { suspect?: string })[];
  // runs seen in the event stream, plus the stored evidence rows (which also hold the matching calculations); a run is listed once
  const sims: (Test & { suspect?: string })[] = [...live, ...tests.filter((t) => !live.some((x) => x.id === t.id))].sort((a, b) => (parseInt(String(a.id).replace(/\D/g, "")) || 0) - (parseInt(String(b.id).replace(/\D/g, "")) || 0));
  const invs = state.investigationOrder.map((id) => state.investigations[id] as Investigation);
  const clearedTotal = state.cleared.reduce((n, c) => n + c.count, 0);
  if (!invs.length && !state.cleared.length)
    return <p className="empty">The board fills in when the agent opens an investigation: each suspect, what was tested on it, and the verdict. Nothing is shown before then.</p>;
  return (
    <div className="ib">
      {invs.map((inv) => {
        const tally = { ruled_out: 0, confirmed: 0, inconclusive: 0, other: 0 } as Record<string, number>;
        inv.suspects.forEach((s) => { tally[s.status in tally ? s.status : "other"]++; });
        return (
          <section key={inv.id} className="ib-inv">
            <header><span className="idx">INVESTIGATION {inv.code}</span>{inv.outcome && <span className="stamp">{String(inv.outcome).replace(/_/g, " ")}</span>}</header>
            <p className="serif ib-q">{inv.question}</p>
            <p className="idx ib-tally">{tally.confirmed} CONFIRMED / {tally.ruled_out} RULED OUT / {tally.inconclusive} INCONCLUSIVE / {tally.other} OTHER</p>
            <ul className="ib-list">{inv.suspects.map((s) => (<SuspectCard key={s.id} s={s} sims={sims} />))}</ul>
            {story.filter((r) => r.investigation === inv.code).length > 0 && <ScreenConfirm rounds={story.filter((r) => r.investigation === inv.code)} />}
          </section>
        );
      })}
      {state.cleared.length > 0 && (
        <section className="ib-cleared">
          <button className="btn sm ghost" onClick={() => setShowCleared(!showCleared)} aria-expanded={showCleared}>{showCleared ? "Hide" : "Show"} checked and cleared ({clearedTotal} results)</button>
          <p className="idx">CHECKED AND CLEARED: JUDGED HARMLESS ON THIS BOARD. THE REASON IS THE AGENT&apos;S OWN.</p>
          {showCleared && (
            <ul className="ib-clr">{state.cleared.map((c) => (<li key={c.id}><span className="mono">{c.rule ?? "candidates"}</span><span className="num">{c.count}</span><span className="dim">{c.reason}</span></li>))}</ul>
          )}
        </section>
      )}
    </div>
  );
}
