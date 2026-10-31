"use client";
import Link from "next/link";
import type { ReactNode } from "react";
import { tabProps, tablistKeys } from "../../lib/hooks";
import { useAppEnv } from "../../lib/demo";
import { DemoNotice, FixtureBadge, Icon, Logo } from "../ui";
import { RunsList } from "./RunsList";

export type ViewId = "findings" | "result" | "investigation" | "sims" | "chat";

export const VIEWS: Record<ViewId, { title: string; icon: string }> = {
  findings: { title: "Findings", icon: "flag" },
  result: { title: "Antenna result", icon: "gauge" },
  investigation: { title: "Investigation", icon: "search" },
  sims: { title: "Simulations", icon: "wave" },
  chat: { title: "Chat", icon: "chat" },
};

export interface NavEntry { id: ViewId; count?: number | null; dot?: boolean }

export function Sidebar({ projectId, projectName, summary, status, nav, view, onView, reviewId, revision, progress }: {
  projectId?: string; projectName: string; summary: string; status: string; nav: NavEntry[]; view: ViewId; onView: (v: ViewId) => void;
  reviewId: string; revision: number; progress?: ReactNode;
}) {
  const { demo } = useAppEnv();
  return (
    <aside className="ws-side" aria-label="Review">
      <Link href="/" aria-label="TrustPCB home" className="ws-logo"><Logo size={20} /></Link>
      <div className="ws-id">
        {projectId ? <Link className="navitem" href={`/projects/${projectId}`} title="Back to the project"><Icon name="back" size={17} /><span className="ws-ellip">{projectName}</span></Link> : <span className="navitem"><Icon name="back" size={17} />{projectName}</span>}
        <p>{summary}</p>
        <p className="ws-status">{status}</p>
      </div>
      <nav className="ws-nav" role="tablist" aria-orientation="vertical" aria-label="Review views" onKeyDown={tablistKeys(true)}>
        {nav.map((n) => (
          <button key={n.id} id={`ws-tab-${n.id}`} {...tabProps(view === n.id)} aria-controls="ws-panel" className="navitem" onClick={() => onView(n.id)}>
            <Icon name={VIEWS[n.id].icon} size={17} />
            {VIEWS[n.id].title}
            {n.dot && <i className="ws-dot" aria-label="Waiting for your approval" />}
            {n.count != null && <small>{n.count}</small>}
          </button>
        ))}
      </nav>
      <nav className="ws-nav" aria-label="Elsewhere">
        <span className="navlabel">Open</span>
        <Link className="navitem" href={`/reviews/${reviewId}/rf`}><Icon name="wave" size={17} />RF lab</Link>
        <Link className="navitem" href={`/reviews/${reviewId}/sim`}><Icon name="flame" size={17} />Simulation lab</Link>
        {revision > 0 && <Link className="navitem" href={`/reviews/${reviewId}/report`}><Icon name="file" size={17} />Report<small>r{revision}</small></Link>}
        {!demo && <Link className="navitem" href="/settings"><Icon name="sliders" size={17} />Settings</Link>}
      </nav>
      <RunsList exclude={reviewId} />
      <div className="ws-foot">
        {progress}
        <FixtureBadge label="Fixture replay" />
        <DemoNotice />
      </div>
    </aside>
  );
}
