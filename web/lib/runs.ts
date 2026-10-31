"use client";
// Reviews that are running or waiting for a free review slot on this instance, so a user can switch away and come back.
import { useEffect, useState } from "react";
import { apiGet, apiSend } from "./live";

export interface RunRow {
  review_id: string; project_id: string; project_name: string | null; version_id: string; version_label: string; depth: string;
  phase: string | null; started_at: string; state: "running" | "queued"; position: number | null; waiting_for_solver: boolean;
}
export interface RunsInfo {
  /** TRUSTPCB_MAX_REVIEWS */ limit: number; working: number; waiting: number; runs: RunRow[];
  solver: { running_review: string | null; waiting: number } | null;
}

export const listRuns = () => apiGet<RunsInfo>("runs");
/** Run an interrupted review again: a new review of the same version, goal and depth, reusing every stored simulation. */
export const restartReview = (reviewId: string) => apiSend<{ id: string }>("POST", `reviews/${reviewId}/restart`);

/** The live runs, refreshed every few seconds while the page is visible. null until the first answer, and in fixture mode (no such route). */
export function useRuns(everyMs = 4000): RunsInfo | null {
  const [info, setInfo] = useState<RunsInfo | null>(null);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (typeof document === "undefined" || !document.hidden) {
        try { const next = await listRuns(); if (alive) setInfo(next); } catch { /* the list is a convenience: the review itself does not depend on it */ }
      }
      if (alive) timer = setTimeout(tick, everyMs);
    };
    void tick();
    return () => { alive = false; if (timer) clearTimeout(timer); };
  }, [everyMs]);
  return info;
}

const PHASE_NAMES = ["Intake", "Baseline", "Understand", "Review", "Investigate", "Confirm", "Report"];
/** One short line for a run in a list: where it is, or its place in the queue. */
export function runStateText(r: RunRow): string {
  if (r.state === "queued") return r.position ? `Queued, place ${r.position}` : "Queued";
  if (r.waiting_for_solver) return "Waiting for the solver";
  const n = r.phase == null ? NaN : Number(r.phase);
  return Number.isFinite(n) && PHASE_NAMES[n] ? `Running, ${PHASE_NAMES[n].toLowerCase()}` : "Running";
}
