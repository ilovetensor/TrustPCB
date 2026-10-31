"use client";
import "./pages.css";
import "../board2d/board2d.css";
import Link from "next/link";
import { useEffect, useState } from "react";
import { getProject, getVersionRf, type ProjectDetail, type VersionRf } from "../../lib/api";
import type { Severity } from "../../lib/types";
import { Icon, Panel, SevBar, TopBar } from "../ui";
import { useAppEnv } from "../../lib/demo";
import { PowerBar } from "../rf/PowerPair";

const total = (c: Record<Severity, number>) => c.critical + c.high + c.medium + c.low + c.info;
const STATE: Record<string, string> = { complete: "Complete", complete_with_limits: "Complete, limits", running: "Running", agent_stopped: "Agent stopped", queued: "Queued", failed: "Failed", canceled: "Stopped", interrupted: "Interrupted", no_review: "No review yet" };

function RfStatus({ r, loading }: { r: VersionRf | null | undefined; loading: boolean }) {
  if (loading) return <span className="dim">reading</span>;
  if (!r || r.status === "not_tested") return <><span className="stamp">Not tested</span>{r?.reason ? ` ${r.reason}` : ""}</>;
  if (r.status === "screen_only") return <><span className="stamp">Screen only</span> no absolute result</>;
  return <>Tested: {r.worst_s11_db != null ? `worst S11 ${r.worst_s11_db} dB` : ""}{r.accepted_worst_pct != null ? `, ${r.accepted_worst_pct} % accepted at the worst point` : ""}. Target {r.target_db} dB <b>{r.meets_target ? "met" : "not met"}</b>.</>;
}

export function Project({ id }: { id: string }) {
  const { demo } = useAppEnv();
  const [data, setData] = useState<ProjectDetail | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { getProject(id).then(setData).catch((e) => { setError(e instanceof Error ? e.message : "Could not load the project."); setData(null); }); }, [id]);
  // the confirmed as-built result of each reviewed version, from its report: not tested stays not tested
  const [rf, setRf] = useState<Record<string, VersionRf | null>>({});
  useEffect(() => {
    if (!data) return;
    let live = true;
    for (const v of data.versions) if (v.review_id) void getVersionRf(v.review_id).then((r) => { if (live) setRf((prev) => ({ ...prev, [v.id]: r })); });
    return () => { live = false; };
  }, [data]);
  if (data === undefined) return <div className="pg"><TopBar current="projects" /></div>;
  if (data === null) return <div className="pg"><TopBar current="projects" /><main id="main" tabIndex={-1} className="pg-main"><h1 className="heading">{error ? "Could not load this project" : "No such project"}</h1>{error && <p className="dim">{error}</p>}<Link href="/projects" className="btn"><Icon name="back" size={14} /> Back to projects</Link></main></div>;
  const { project, versions } = data;
  const reviewed = versions.filter((v) => v.counts);
  const trend = reviewed.map((v) => total(v.counts!));
  const max = Math.max(...trend, 1);
  const last = versions.filter((v) => v.review_id).pop();
  const pair = reviewed.length > 1 ? [reviewed[reviewed.length - 2], reviewed[reviewed.length - 1]] : null;
  return (
    <div className="pg">
      <TopBar current="projects" />
      <main id="main" tabIndex={-1} className="pg-main">
        <div className="pg-head">
          <div>
            <Link href="/projects" className="pg-back"><Icon name="back" size={13} /> Back to projects</Link>
            <h1 className="heading">{project.name}</h1>
            <p className="pg-sub">{project.kind}</p>
          </div>
          <span className="grow" />
          {pair && <Link className="btn" href={`/compare?from=${pair[0].id}&to=${pair[1].id}`}><Icon name="compare" size={14} /> Compare versions</Link>}
          {!demo && <Link className="btn primary" href={`/upload?project=${project.id}`}><Icon name="upload" size={14} /> Upload a new version</Link>}
        </div>

        <section>
          <h2 className="pg-h2">Timeline</h2>
          <div className="timeline">
            {versions.map((v, i) => (
              <Link key={v.id} href={v.review_id ? `/reviews/${v.review_id}` : demo ? `/projects/${project.id}` : `/upload?version=${v.id}`} className={`tl-node ${i === versions.length - 1 ? "latest" : ""}`}>
                <span className="idx">{v.at.slice(0, 10)}, {v.review_id ? `review ${v.review_id.slice(0, 6)}` : "no review"}</span>
                <i /><b>{v.label}</b>
                {v.counts ? <SevBar counts={v.counts} size="sm" /> : <span className="idx">{v.status === "failed" ? "Upload failed" : demo ? "No review" : "Start a review"}</span>}
                <p>{v.note || `${v.counts ? total(v.counts) : 0} open findings. ${STATE[v.state]}.`}</p>
              </Link>
            ))}
          </div>
        </section>

        <div className="cards2">
          <Panel className="card" title="Open findings per version">
            {trend.length === 0 ? <p className="empty">No finished review yet.</p> : (
              <svg viewBox="0 0 360 150" width="100%" role="img" aria-label="Open findings per version">
                {trend.map((t, i) => {
                  const x = 60 + i * (trend.length > 1 ? 260 / (trend.length - 1) : 100);
                  const h = (t / max) * 100;
                  return (
                    <g key={i}>
                      <rect x={x - 16} y={120 - h} width="32" height={h} rx="7" fill="var(--paper)" opacity={i === trend.length - 1 ? 0.85 : 0.25} />
                      <text x={x} y={120 - h - 6} textAnchor="middle" className="rl-t" fill="var(--paper)">{t}</text>
                      <text x={x} y="138" textAnchor="middle" className="rl-t">{reviewed[i].label}</text>
                    </g>
                  );
                })}
              </svg>
            )}
          </Panel>
          {project.rf !== "n/a" && (
            <Panel className="card" title="Accepted power by version">
              {reviewed.every((v) => rf[v.id]?.status !== "tested") ? (
                <p className="empty">
                  <span className="stamp">Not tested</span><br />
                  {reviewed.some((v) => rf[v.id] === undefined) ? "Reading the stored results." : (reviewed.map((v) => rf[v.id]?.reason).find(Boolean) ?? "No simulation result is stored for this project. The match by version appears here once a Standard or Deep review runs the RF lab.")}
                </p>
              ) : (
                <div className="pg-rf">
                  {reviewed.map((v) => {
                    const r = rf[v.id];
                    return r?.status === "tested" ? (
                      <div key={v.id} className="pg-rf-row">
                        <PowerBar value={r.accepted_worst_pct} label={v.label} sub="as built, worst point of the band" tone={v === reviewed[reviewed.length - 1] ? "now" : "before"} size="sm" />
                        <p className="pg-rf-line">
                          worst S11 {r.worst_s11_db != null ? `${r.worst_s11_db.toFixed(2).replace(/\.?0+$/, "").replace("-", "−")} dB` : "-"}
                          {r.accepted_mean_pct != null && <> / mean accepted {r.accepted_mean_pct} %</>}
                          {r.target_db != null && <> / target {r.target_db} dB <b>{r.meets_target ? "met" : "not met"}</b></>}
                        </p>
                      </div>
                    ) : (
                      <div key={v.id} className="pg-rf-row"><span className="idx">{v.label}</span><p className="pg-rf-line"><span className="stamp">Not tested</span> {r?.reason ?? (r === undefined ? "reading" : "no stored result")}</p></div>
                    );
                  })}
                  <p className="dim pg-rf-note">Accepted power is 100 x (1 - |S11|^2): a mismatch figure only, not radiated power or efficiency. Confirm-quality simulation of the board as built.</p>
                </div>
              )}
            </Panel>
          )}
          <Panel className="card" title="Latest review">
            {last?.review_id ? (
              <>
                <dl className="kv">
                  <dt>Review</dt><dd className="num">{last.review_id.slice(0, 6)}</dd>
                  <dt>Version</dt><dd>{last.label}</dd>
                  <dt>State</dt><dd>{STATE[last.state]}</dd>
                  <dt>Open</dt><dd>{last.counts ? <SevBar counts={last.counts} size="sm" /> : "-"}</dd>
                  <dt>RF</dt><dd>{project.rf === "n/a" ? "No radio part detected" : <RfStatus r={rf[last.id]} loading={last.review_id ? rf[last.id] === undefined : false} />}</dd>
                </dl>
                <div style={{ display: "flex", gap: 8, marginTop: 16, flexWrap: "wrap" }}>
                  <Link className="btn sm primary" href={`/reviews/${last.review_id}`}><Icon name="board" size={13} /> Open review</Link>
                  <Link className="btn sm" href={`/reviews/${last.review_id}/report`}><Icon name="file" size={13} /> Read the report</Link>
                  <Link className="btn sm" href={`/reviews/${last.review_id}/rf`}><Icon name="wave" size={13} /> Open RF lab</Link>
                </div>
              </>
            ) : <p className="empty">No review has been started for this project.</p>}
          </Panel>
        </div>
      </main>
    </div>
  );
}
