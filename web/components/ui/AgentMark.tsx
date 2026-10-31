"use client";
import { useCallback, useState } from "react";
import { agentFor, agentLabel, type AgentKind } from "../../lib/agents.mjs";
import "./chrome.css";

/** Logo files that failed to load once: the neutral icon is used from then on, without asking again. */
const missing = new Set<string>();

/**
 * The only place an agent logo is drawn. Pass the runner that wrote the text (a review's runner); the registry in
 * lib/agents.mjs decides the name and the logo. An unknown or missing runner, or a logo file that is not there, gives
 * the plain neutral icon, so a mark can never claim a brand the runner does not have.
 */
export function AgentMark({ runner, kind = "answered", size = 14, className = "" }: { runner?: string | null; kind?: AgentKind; size?: number; className?: string }) {
  const agent = agentFor(runner);
  const label = agentLabel(runner, kind);
  const [, bump] = useState(0);
  const logo = agent.logo && !missing.has(agent.logo) ? agent.logo : null;
  const failed = useCallback(() => { if (agent.logo) { missing.add(agent.logo); bump((n) => n + 1); } }, [agent.logo]);
  // an image that failed before React attached onError (server-rendered page) is caught here
  const ref = useCallback((img: HTMLImageElement | null) => { if (img && img.complete && img.naturalWidth === 0) failed(); }, [failed]);
  const cls = `agent-mark${kind === "working" ? " working" : ""}${className ? ` ${className}` : ""}`;
  const common = { className: cls, "data-agent": agent.id, "data-brand": logo ? agent.brand ?? "" : "", style: { width: size, height: size } };
  if (logo)
    return (
      <span {...common}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img ref={ref} src={logo} alt={label} title={label} width={size} height={size} onError={failed} draggable={false} />
      </span>
    );
  return (
    <span {...common} role="img" aria-label={label} title={label}>
      <svg viewBox="0 0 16 16" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        <rect x="2.5" y="5" width="11" height="8" rx="2.5" />
        <path d="M8 5V2.8" />
        <path d="M5.8 9v.6M10.2 9v.6" />
      </svg>
    </span>
  );
}
