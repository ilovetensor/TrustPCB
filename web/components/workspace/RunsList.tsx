"use client";
import Link from "next/link";
import { runStateText, useRuns, type RunRow } from "../../lib/runs";
import { Icon } from "../ui/icons";

/** The reviews running or waiting on this instance, each a link, so switching to another review or project never loses sight of a run. Renders nothing when there are none. */
export function RunsList({ exclude, project, heading = "Running now" }: { exclude?: string; /** only the runs of this project */ project?: string; heading?: string }) {
  const info = useRuns();
  const rows = (info?.runs ?? []).filter((r) => r.review_id !== exclude && (!project || r.project_id === project));
  if (rows.length === 0) return null;
  return (
    <nav className="ws-nav ws-runs" aria-label={heading}>
      <span className="navlabel">{heading}</span>
      {rows.map((r) => <RunLink key={r.review_id} run={r} />)}
    </nav>
  );
}

export function RunLink({ run }: { run: RunRow }) {
  return (
    <Link className="navitem" href={`/reviews/${run.review_id}`} title={`Open the review of ${run.project_name ?? "this board"}, ${run.version_label}`}>
      <Icon name={run.state === "queued" ? "pause" : "play"} size={17} />
      <span className="ws-ellip">{run.project_name ?? "Board"} {run.version_label}</span>
      <small>{runStateText(run)}</small>
    </Link>
  );
}
