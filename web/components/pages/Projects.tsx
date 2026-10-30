"use client";
import "./pages.css";
import Link from "next/link";
import { useEffect, useState } from "react";
import { listProjects } from "../../lib/api";
import type { ProjectSummary } from "../../lib/types";
import { Sparkline, SevBar, TopBar, Icon } from "../ui";

const STATE: Record<string, string> = { complete: "Complete", complete_with_limits: "Complete, limits", running: "Running", agent_stopped: "Agent stopped", queued: "Queued" };
const RF: Record<string, string> = { pass: "RF in band", fail: "RF off target", not_tested: "RF not tested", "n/a": "No wireless" };

export function Projects() {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  useEffect(() => { listProjects().then(setProjects); }, []);
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
                <span className="mono" style={{ fontSize: 12, color: p.rf === "fail" ? "var(--signal)" : "var(--paper-2)" }}>{RF[p.rf]}</span>
                <span><SevBar counts={p.counts} size="sm" /></span>
                <span><Sparkline values={p.trend} /></span>
                <span className="mono" style={{ fontSize: 12 }}>{p.latest_version} / {p.versions} ver</span>
                <span className={`tbl-state ${p.state}`}><i />{STATE[p.state]}</span>
              </Link>
            ))}
            {!projects && <p className="empty dim" style={{ padding: 24 }}>Loading projects.</p>}
          </div>
          <aside>
            <span className="idx">RECENT ACTIVITY</span>
            <ul className="feed" style={{ marginTop: 14 }}>
              <li className="sig"><b>nRF52840 RF IoT Node</b> v2 review finished. 1 high, 2 medium. Report r1 ready.</li>
              <li><b>2.5 GHz Patch Antenna</b> v1 resonates in band. 3 notes.</li>
              <li><b>USB-C Hub Rev B</b> v3 rechecked. 2 findings carried over, 1 fixed.</li>
              <li><b>Sensor Pod</b> review stopped at the time budget. Report lists what was not tested.</li>
            </ul>
          </aside>
        </div>
      </main>
    </div>
  );
}
