"use client";
import "./pages.css";
import Link from "next/link";
import { useEffect, useState } from "react";
import { listProjects } from "../../lib/api";
import type { ProjectSummary } from "../../lib/types";
import { Sparkline, SevBar, TopBar, Icon } from "../ui";

const STATE: Record<string, string> = { complete: "Complete", complete_with_limits: "Complete, limits", running: "Running", agent_stopped: "Agent stopped", queued: "Queued", failed: "Failed", canceled: "Stopped", no_review: "No review yet" };
const RF: Record<string, string> = { pass: "RF in band", fail: "RF off target", not_tested: "RF not tested", "n/a": "No radio" };

export function Projects() {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { listProjects().then(setProjects).catch((e) => setError(e instanceof Error ? e.message : "Could not load projects.")); }, []);
  const total = projects?.reduce((s, p) => s + p.counts.critical + p.counts.high + p.counts.medium + p.counts.low + p.counts.info, 0) ?? 0;
  return (
    <div className="pg">
      <TopBar current="projects" />
      <main className="pg-main">
        <div className="pg-head">
          <div>
            <span className="idx">DASHBOARD / {projects?.length ?? 0} BOARDS / {total} OPEN FINDINGS</span>
            <h1 className="serif">Projects</h1>
          </div>
          <span className="grow" />
          <Link className="btn primary" href="/upload"><Icon name="upload" size={13} /> New board</Link>
        </div>
        <div className="pg-cols">
          <div className="tbl" role="table" aria-label="Projects">
            <div className="tbl-row head" role="row">
              <span className="idx">ID</span><span className="idx">BOARD</span><span className="idx">LATEST RESULT</span><span className="idx">OPEN BY SEVERITY</span><span className="idx">TREND</span><span className="idx">VERSION</span><span className="idx">STATE</span>
            </div>
            {projects?.map((p, i) => (
              <Link key={p.id} href={`/projects/${p.id}`} className="tbl-row rise" style={{ animationDelay: `${i * 70}ms` }} role="row">
                <span className="idx">P-{String(i + 1).padStart(3, "0")}</span>
                <span><div className="tbl-name">{p.name}</div><div className="tbl-kind">{p.kind}</div></span>
                <span className="mono" style={{ fontSize: 12, color: p.rf === "fail" ? "var(--signal)" : "var(--paper-2)" }}>{p.rf_note ?? RF[p.rf]}</span>
                <span><SevBar counts={p.counts} size="sm" /></span>
                <span><Sparkline values={p.trend} /></span>
                <span className="mono" style={{ fontSize: 12 }}>{p.latest_version} / {p.versions} ver</span>
                <span className={`tbl-state ${p.state}`}><i />{STATE[p.state]}</span>
              </Link>
            ))}
            {!projects && !error && <p className="empty dim" style={{ padding: 24 }}>Loading projects.</p>}
            {error && <p className="empty" role="alert" style={{ padding: 24, color: "var(--sev-high)" }}>{error}</p>}
            {projects && projects.length === 0 && <p className="empty dim" style={{ padding: 24 }}>No boards yet. <Link href="/upload" className="signal">Upload one</Link> to start a review.</p>}
          </div>
          <aside>
            <span className="idx">RECENT ACTIVITY</span>
            <ul className="feed" style={{ marginTop: 14 }}>
              {(projects ?? []).slice(0, 6).map((p, i) => {
                const n = p.counts.critical + p.counts.high + p.counts.medium + p.counts.low + p.counts.info;
                return (
                  <li key={p.id} className={i === 0 ? "sig" : undefined}>
                    <b>{p.name}</b> {p.latest_version}: {STATE[p.state].toLowerCase()}{p.state === "no_review" ? "." : `. ${n} open finding${n === 1 ? "" : "s"}.`}
                  </li>
                );
              })}
              {projects && projects.length === 0 && <li className="dim">Nothing yet.</li>}
            </ul>
          </aside>
        </div>
      </main>
    </div>
  );
}
