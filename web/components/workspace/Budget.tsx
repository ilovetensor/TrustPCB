"use client";
import { fmtClock } from "../../lib/review-view";
import type { Budget } from "../../lib/review-reducer.mjs";
import { Icon } from "../ui/icons";

function Cell({ label, value, of, frac, sig, title }: { label: string; value: string; of?: string; frac: number; sig?: boolean; title?: string }) {
  return (
    <div className="bg-cell" title={title}>
      <span className="idx">{label}</span>
      <span className="bg-v num">{value}{of && <small>/{of}</small>}</span>
      <div className={`meter ${sig ? "sig" : ""}`}><b style={{ width: `${Math.max(0, Math.min(1, frac)) * 100}%` }} /></div>
    </div>
  );
}

/** Time, solver time and agent turns, from the live budget event. Nothing is shown that the backend did not report. */
export function BudgetInstrument({ budget, status, canStop, onStop }: { budget: Budget | null; status: string; canStop: boolean; onStop: () => void }) {
  const b = budget;
  const wall = b?.wall_s, solver = b?.solver_s, turns = b?.turns;
  const solverOff = !!solver && solver.limit === 0;
  return (
    <div className="bg">
      <Cell label="TIME LEFT" value={wall ? fmtClock(wall.left ?? wall.limit - wall.used) : "--:--"} frac={wall && wall.limit ? (wall.left ?? wall.limit - wall.used) / wall.limit : 1} />
      <Cell label="SOLVER" value={solver ? (solverOff ? "none" : fmtClock(solver.left ?? solver.limit - solver.used)) : "--:--"} frac={solver && solver.limit ? (solver.left ?? solver.limit - solver.used) / solver.limit : 0} title={solverOff ? "This review depth runs no simulation." : undefined} />
      <Cell label="TURNS" value={turns ? String(turns.used) : "-"} of={turns ? String(turns.limit) : undefined} frac={turns && turns.limit ? 1 - turns.used / turns.limit : 1} />
      <button className="btn danger sm bg-stop" onClick={onStop} disabled={!canStop} aria-label="Stop the review"><Icon name="stop" size={11} /> Stop</button>
    </div>
  );
}
