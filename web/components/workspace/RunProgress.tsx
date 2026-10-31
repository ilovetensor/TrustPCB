"use client";
import { PHASES, type Budget } from "../../lib/review-reducer.mjs";
import { fmtClock } from "../../lib/review-view";
import { Icon } from "../ui/icons";

/** Where a running review is, in plain words, with what is left of its budget and a way to stop it. Only shown while it runs. */
export function RunProgress({ phase, budget, refine, canStop, onStop }: {
  phase: number; budget: Budget | null; refine: { round: number; rounds: number | null } | null; canStop: boolean; onStop: () => void;
}) {
  const last = PHASES.length - 1;
  const wall = budget?.wall_s, solver = budget?.solver_s, turns = budget?.turns;
  const solverOff = !!solver && solver.limit === 0;
  const rows: [string, string, string | undefined][] = [];
  if (wall) rows.push(["Time left", fmtClock(wall.left ?? wall.limit - wall.used), undefined]);
  if (solver) rows.push(["Solver left", solverOff ? "none" : fmtClock(solver.left ?? solver.limit - solver.used), solverOff ? "This review depth runs no simulation." : undefined]);
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
