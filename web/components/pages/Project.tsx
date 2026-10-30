"use client";
import "./pages.css";
import Link from "next/link";
import { useEffect, useState } from "react";
import { getProject, resolveCurve, TARGET_BAND } from "../../lib/api";
import type { VersionRow } from "../../lib/fixtures/projects";
import type { ProjectSummary, Severity } from "../../lib/types";
import { TuningGraph } from "../rf/charts";
import { Icon, Panel, SevBar, TopBar } from "../ui";

const total = (c: Record<Severity, number>) => c.critical + c.high + c.medium + c.low + c.info;

export function Project({ id }: { id: string }) {
  const [data, setData] = useState<{ project: ProjectSummary; versions: VersionRow[] } | null | undefined>(undefined);
  useEffect(() => { getProject(id).then(setData); }, [id]);
  if (data === undefined) return <div className="pg"><TopBar current="projects" /></div>;
  if (data === null) return <div className="pg"><TopBar current="projects" /><main className="pg-main"><h1 className="serif">No such project</h1><Link href="/projects" className="btn">Back to projects</Link></main></div>;
  const { project, versions } = data;
  const trend = versions.map((v) => total(v.counts));
  const max = Math.max(...trend, 1);
  const withF0 = versions.filter((v) => v.f0_ghz);
  const curves = [resolveCurve("v1"), resolveCurve("base")].filter(Boolean) as NonNullable<ReturnType<typeof resolveCurve>>[];
  return (
    <div className="pg">
      <TopBar current="projects" />
      <main className="pg-main">
        <div className="pg-head">
          <div>
            <Link href="/projects" className="idx">PROJECTS /</Link> <span className="idx">{project.id.toUpperCase()}</span>
            <h1 className="serif">{project.name}</h1>
            <p className="pg-sub">{project.kind}</p>
          </div>
          <span className="grow" />
          {versions.length > 1 && <Link className="btn" href="/compare"><Icon name="compare" size={13} /> Compare versions</Link>}
          <Link className="btn primary" href="/upload"><Icon name="upload" size={13} /> Upload new version</Link>
        </div>

        <Panel idx="VERSIONS" title="Timeline" delay={0}>
          <div className="timeline">
            {versions.map((v, i) => (
              <Link key={v.id} href={`/reviews/${v.review_id}`} className={`tl-node ${i === versions.length - 1 ? "latest" : ""}`}>
                <span className="idx">{new Date(v.at).toISOString().slice(0, 10)} / {v.review_id.toUpperCase()}</span>
                <i /><b>{v.label}</b>
                <SevBar counts={v.counts} size="sm" />
                <p>{v.note || `${total(v.counts)} findings`}</p>
              </Link>
            ))}
          </div>
        </Panel>

        <div className="cards2">
          <Panel className="card" idx="TREND" title="Open findings per version" delay={120}>
            <svg viewBox="0 0 360 150" width="100%" role="img" aria-label="Open findings per version">
              {[0, 0.5, 1].map((k) => (<line key={k} x1="30" x2="350" y1={120 - k * 100} y2={120 - k * 100} stroke="var(--hair)" />))}
              {trend.map((t, i) => {
                const x = 60 + i * (versions.length > 1 ? 260 / (versions.length - 1) : 0);
                const h = (t / max) * 100;
                return (
                  <g key={i}>
                    <rect x={x - 16} y={120 - h} width="32" height={h} fill="none" stroke="var(--paper)" />
                    <rect x={x - 16} y={120 - h} width="32" height={h} fill="var(--paper)" opacity={i === trend.length - 1 ? 0.25 : 0.08} />
                    <text x={x} y={120 - h - 6} textAnchor="middle" className="rl-t" fill="var(--paper)">{t}</text>
                    <text x={x} y="138" textAnchor="middle" className="rl-t">{versions[i].label}</text>
                  </g>
                );
              })}
            </svg>
          </Panel>
          {project.rf !== "n/a" && curves.length > 0 && (
            <Panel className="card" idx="RF" title="Resonance by version" delay={200}>
              <TuningGraph curves={curves.map((c, i) => ({ id: String(i), label: i ? "v2" : "v1", ...c }))} band={TARGET_BAND} height={160} />
              <p className="mono dim" style={{ fontSize: 11, marginTop: 8 }}>{withF0.map((v) => `${v.label} ${v.f0_ghz?.toFixed(3)} GHz`).join("  /  ")} &nbsp; target {TARGET_BAND.lo_ghz} to {TARGET_BAND.hi_ghz} GHz. Fixture curves.</p>
            </Panel>
          )}
          <Panel className="card" idx="LATEST" title="Latest review" delay={280}>
            <dl className="kv">
              <dt>Review</dt><dd className="mono">{project.latest_review_id.toUpperCase()}</dd>
              <dt>Version</dt><dd>{project.latest_version}</dd>
              <dt>Open</dt><dd><SevBar counts={project.counts} size="sm" /></dd>
              <dt>RF</dt><dd>{project.rf === "fail" ? "Resonance below target band" : project.rf === "pass" ? "In band" : project.rf === "not_tested" ? "Not tested" : "No wireless part"}</dd>
            </dl>
            <div style={{ display: "flex", gap: 8, marginTop: 16, flexWrap: "wrap" }}>
              <Link className="btn sm" href={`/reviews/${project.latest_review_id}`}>Open workspace</Link>
              <Link className="btn sm" href={`/reviews/${project.latest_review_id}/report`}>Report</Link>
              <Link className="btn sm" href={`/reviews/${project.latest_review_id}/rf`}>RF lab</Link>
            </div>
          </Panel>
        </div>
      </main>
    </div>
  );
}
