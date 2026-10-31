"use client";
import "./pages.css";
import Link from "next/link";
import { useEffect, useState } from "react";
import { getProject, type ProjectDetail } from "../../lib/api";
import type { Severity } from "../../lib/types";
import { Icon, Panel, SevBar, TopBar } from "../ui";

const total = (c: Record<Severity, number>) => c.critical + c.high + c.medium + c.low + c.info;
const STATE: Record<string, string> = { complete: "Complete", complete_with_limits: "Complete, limits", running: "Running", agent_stopped: "Agent stopped", queued: "Queued", failed: "Failed", canceled: "Stopped", no_review: "No review yet" };

export function Project({ id }: { id: string }) {
  const [data, setData] = useState<ProjectDetail | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { getProject(id).then(setData).catch((e) => { setError(e instanceof Error ? e.message : "Could not load the project."); setData(null); }); }, [id]);
  if (data === undefined) return <div className="pg"><TopBar current="projects" /></div>;
  if (data === null) return <div className="pg"><TopBar current="projects" /><main className="pg-main"><h1 className="serif">{error ? "Could not load this project" : "No such project"}</h1>{error && <p className="dim">{error}</p>}<Link href="/projects" className="btn">Back to projects</Link></main></div>;
  const { project, versions } = data;
  const reviewed = versions.filter((v) => v.counts);
  const trend = reviewed.map((v) => total(v.counts!));
  const max = Math.max(...trend, 1);
  const last = versions.filter((v) => v.review_id).pop();
  const pair = reviewed.length > 1 ? [reviewed[reviewed.length - 2], reviewed[reviewed.length - 1]] : null;
  return (
    <div className="pg">
      <TopBar current="projects" />
      <main className="pg-main">
        <div className="pg-head">
          <div>
            <Link href="/projects" className="idx">PROJECTS /</Link> <span className="idx">{project.id.slice(0, 8).toUpperCase()}</span>
            <h1 className="serif">{project.name}</h1>
            <p className="pg-sub">{project.kind}</p>
          </div>
          <span className="grow" />
          {pair && <Link className="btn" href={`/compare?from=${pair[0].id}&to=${pair[1].id}`}><Icon name="compare" size={13} /> Compare versions</Link>}
          <Link className="btn primary" href={`/upload?project=${project.id}`}><Icon name="upload" size={13} /> Upload new version</Link>
        </div>

        <Panel idx="VERSIONS" title="Timeline" delay={0}>
          <div className="timeline">
            {versions.map((v, i) => (
              <Link key={v.id} href={v.review_id ? `/reviews/${v.review_id}` : `/upload?version=${v.id}`} className={`tl-node ${i === versions.length - 1 ? "latest" : ""}`}>
                <span className="idx">{v.at.slice(0, 10)} / {v.review_id ? `R-${v.review_id.slice(0, 6).toUpperCase()}` : "NO REVIEW"}</span>
                <i /><b>{v.label}</b>
                {v.counts ? <SevBar counts={v.counts} size="sm" /> : <span className="idx">{v.status === "failed" ? "UPLOAD FAILED" : "START A REVIEW"}</span>}
                <p>{v.note || `${v.counts ? total(v.counts) : 0} open findings. ${STATE[v.state]}.`}</p>
              </Link>
            ))}
          </div>
        </Panel>

        <div className="cards2">
          <Panel className="card" idx="TREND" title="Open findings per version" delay={120}>
            {trend.length === 0 ? <p className="empty">No finished review yet.</p> : (
              <svg viewBox="0 0 360 150" width="100%" role="img" aria-label="Open findings per version">
                {[0, 0.5, 1].map((k) => (<line key={k} x1="30" x2="350" y1={120 - k * 100} y2={120 - k * 100} stroke="var(--hair)" />))}
                {trend.map((t, i) => {
                  const x = 60 + i * (trend.length > 1 ? 260 / (trend.length - 1) : 100);
                  const h = (t / max) * 100;
                  return (
                    <g key={i}>
                      <rect x={x - 16} y={120 - h} width="32" height={h} fill="none" stroke="var(--paper)" />
                      <rect x={x - 16} y={120 - h} width="32" height={h} fill="var(--paper)" opacity={i === trend.length - 1 ? 0.25 : 0.08} />
                      <text x={x} y={120 - h - 6} textAnchor="middle" className="rl-t" fill="var(--paper)">{t}</text>
                      <text x={x} y="138" textAnchor="middle" className="rl-t">{reviewed[i].label}</text>
                    </g>
                  );
                })}
              </svg>
            )}
          </Panel>
          {project.rf !== "n/a" && (
            <Panel className="card" idx="RF" title="Resonance by version" delay={200}>
              <p className="empty"><span className="stamp">Not tested</span><br />No simulation result is stored for this project. Resonance by version appears here once a Standard or Deep review runs the RF lab.</p>
            </Panel>
          )}
          <Panel className="card" idx="LATEST" title="Latest review" delay={280}>
            {last?.review_id ? (
              <>
                <dl className="kv">
                  <dt>Review</dt><dd className="mono">R-{last.review_id.slice(0, 6).toUpperCase()}</dd>
                  <dt>Version</dt><dd>{last.label}</dd>
                  <dt>State</dt><dd>{STATE[last.state]}</dd>
                  <dt>Open</dt><dd>{last.counts ? <SevBar counts={last.counts} size="sm" /> : "-"}</dd>
                  <dt>RF</dt><dd>{project.rf === "n/a" ? "No radio part detected" : "Not tested"}</dd>
                </dl>
                <div style={{ display: "flex", gap: 8, marginTop: 16, flexWrap: "wrap" }}>
                  <Link className="btn sm" href={`/reviews/${last.review_id}`}>Open workspace</Link>
                  <Link className="btn sm" href={`/reviews/${last.review_id}/report`}>Report</Link>
                  <Link className="btn sm" href={`/reviews/${last.review_id}/rf`}>RF lab</Link>
                </div>
              </>
            ) : <p className="empty">No review has been started for this project.</p>}
          </Panel>
        </div>
      </main>
    </div>
  );
}
