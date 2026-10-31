"use client";
import { useEffect, useState } from "react";
import { PHASES, type Budget } from "../../lib/review-reducer.mjs";
import { fmtClock } from "../../lib/review-view";
import { Icon } from "../ui/icons";

/** Where a running review is, in plain words, with what is left of its budget and a way to stop it. Only shown while it runs. */
export function RunProgress({ phase, budget, budgetAt, solving = false, refine, canStop, onStop }: {
  phase: number; budget: Budget | null; budgetAt?: number | null; solving?: boolean; refine: { round: number; rounds: number | null } | null; canStop: boolean; onStop: () => void;
}) {
  // The server reports its budget after each step, and a simulation can run for minutes between two reports.
  // Count the clock on from the report's own time so it does not sit still; the next report replaces the estimate.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);
  const gone = budgetAt ? Math.max(0, (now - budgetAt) / 1000) : 0;
  const last = PHASES.length - 1;
  const wall = budget?.wall_s, solver = budget?.solver_s, turns = budget?.turns;
  const solverOff = !!solver && solver.limit === 0;
  const rows: [string, string, string | undefined][] = [];
  if (wall) rows.push(["Time limit left", fmtClock((wall.left ?? wall.limit - wall.used) - gone), "The most this review may take, not when it will finish."]);
  if (solver) rows.push(["Solver limit left", solverOff ? "none" : fmtClock((solver.left ?? solver.limit - solver.used) - (solving ? gone : 0)), solverOff ? "Nothing is simulated in this review." : undefined]);
  if (turns) rows.push(["Turns", `${turns.used} of ${turns.limit}`, undefined]);
  return (
    <div className="rp" role="group" aria-label="Review progress">
      <b>{phase >= 0 ? `Step ${phase} of ${last}, ${PHASES[phase].label}` : "Starting"}</b>
      <div className="meter" role="progressbar" aria-valuenow={Math.max(phase, 0)} aria-valuemin={0} aria-valuemax={last}><b style={{ width: `${(Math.max(phase, 0) / last) * 100}%` }} /></div>
      {rows.length > 0 && (
        <dl>{rows.map(([k, v, t]) => (<div key={k} title={t}><dt>{k}</dt><dd className="num">{v}</dd></div>))}</dl>
      )}
      {refine && <span className="rp-refine" title="The confirm run missed the target, so the agent is making one more bounded attempt. The step stays at Confirm.">Refine round {refine.round}{refine.rounds ? ` of ${refine.rounds}` : ""}</span>}
      <button className="btn danger sm" onClick={onStop} disabled={!canStop} aria-label="Stop the review"><Icon name="stop" size={12} /> Stop</button>
    </div>
  );
}
