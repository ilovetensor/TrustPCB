"use client";
import "./pages.css";
import Link from "next/link";
import { useEffect, useState } from "react";
import { listProjects } from "../../lib/api";
import type { ProjectSummary } from "../../lib/types";
import { Sparkline, SevBar, TopBar, Icon } from "../ui";
import { useAppEnv } from "../../lib/demo";

const STATE: Record<string, string> = { complete: "Complete", complete_with_limits: "Complete, limits", running: "Running", agent_stopped: "Agent stopped", queued: "Queued", failed: "Failed", canceled: "Stopped", interrupted: "Interrupted", no_review: "No review yet" };
const RF: Record<string, string> = { pass: "RF in band", fail: "RF off target", not_tested: "RF not tested", "n/a": "No radio" };

export function Projects() {
  const { demo } = useAppEnv();
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { listProjects().then(setProjects).catch((e) => setError(e instanceof Error ? e.message : "Could not load projects.")); }, []);
  const total = projects?.reduce((s, p) => s + p.counts.critical + p.counts.high + p.counts.medium + p.counts.low + p.counts.info, 0) ?? 0;
  return (
    <div className="pg">
      <TopBar current="projects" />
      <main id="main" tabIndex={-1} className="pg-main">
        <div className="pg-head">
          <div>
            <h1 className="heading">Projects</h1>
            <p className="pg-sub">{projects ? `${projects.length} ${projects.length === 1 ? "board" : "boards"}, ${total} open ${total === 1 ? "finding" : "findings"}` : "Loading projects."}</p>
          </div>
          <span className="grow" />
          {!demo && <Link className="btn primary" href="/upload"><Icon name="upload" size={14} /> Upload a board</Link>}
        </div>
        <div className="pg-cols">
          <div className="tiles" aria-label="Projects">
            {projects?.map((p, i) => (
              <Link key={p.id} href={`/projects/${p.id}`} className="tile rise" style={{ animationDelay: `${i * 70}ms` }}>
                <div className="tile-top">
                  <div><div className="tile-name">{p.name}</div><div className="tile-kind">{p.kind}</div></div>
                  <span className={`tbl-state ${p.state}`}><i />{STATE[p.state]}</span>
                </div>
                <div className={`tile-rf ${p.rf === "fail" ? "fail" : ""}`}>{p.rf_note ?? RF[p.rf]}</div>
                <div className="tile-row"><SevBar counts={p.counts} size="sm" /><Sparkline values={p.trend} /></div>
                <div className="tile-foot"><span>{p.latest_version}, {p.versions} {p.versions === 1 ? "version" : "versions"}</span><span className="tile-open">Open project <Icon name="arrow" size={13} /></span></div>
              </Link>
            ))}
            {!projects && !error && <p className="empty dim" style={{ padding: 24 }}>Loading projects.</p>}
            {error && <p className="empty" role="alert" style={{ padding: 24, color: "var(--sev-high)" }}>{error}</p>}
            {projects && projects.length === 0 && <p className="empty dim" style={{ padding: 24 }}>No boards yet. {demo ? "This demo has none to show." : <><Link href="/upload" className="signal">Upload one</Link> to start a review.</>}</p>}
          </div>
          <aside>
            <h2 className="pg-h2" style={{ margin: 0 }}>Recent activity</h2>
            <ul className="feed">
              {(projects ?? []).slice(0, 6).map((p, i) => {
                const n = p.counts.critical + p.counts.high + p.counts.medium + p.counts.low + p.counts.info;
                return (
                  <li key={p.id} className={i === 0 ? "sig" : undefined}>
                    <span><b>{p.name}</b> {p.latest_version}: {STATE[p.state].toLowerCase()}{p.state === "no_review" ? "." : `. ${n} open finding${n === 1 ? "" : "s"}.`}</span>
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
