"use client";
import { fmtClock } from "../../lib/review-view";
import type { Budget } from "../../lib/review-reducer.mjs";
import { Icon } from "../ui/icons";

function Cell({ label, value, of, frac, sig }: { label: string; value: string; of: string; frac: number; sig?: boolean }) {
  return (
    <div className="bg-cell">
      <span className="idx">{label}</span>
      <span className="bg-v num">{value}{of && <small>/{of}</small>}</span>
      <div className={`meter ${sig ? "sig" : ""}`}><b style={{ width: `${Math.max(0, Math.min(1, frac)) * 100}%` }} /></div>
    </div>
  );
}

export function BudgetInstrument({ budget, status, onStop }: { budget: Budget | null; status: string; onStop: () => void }) {
  const b = budget;
  const running = status === "running";
  return (
    <div className="bg">
      <Cell label="TIME LEFT" value={b ? fmtClock(b.wall_left_s) : "--:--"} of="" frac={b ? b.wall_left_s / b.wall_total_s : 1} />
      <Cell label="SOLVER" value={b ? fmtClock(b.solver_left_s) : "--:--"} of="" frac={b ? b.solver_left_s / b.solver_total_s : 1} />
      <Cell label="RUNS LEFT" value={b ? String(b.sims_left) : "-"} of={b ? String(b.sims_total) : "-"} frac={b ? b.sims_left / b.sims_total : 1} />
      <button className="btn danger sm bg-stop" onClick={onStop} disabled={!running} aria-label="Stop the review"><Icon name="stop" size={11} /> Stop</button>
    </div>
  );
}
