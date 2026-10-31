import "./chrome.css";
import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";
import type { EvidenceClass, Severity } from "../../lib/types";
import { DATA_SOURCE } from "../../lib/api";
import { useAppEnv } from "../../lib/demo";
import { Icon } from "./icons";

export { Icon };

/* Mark: the callout balloon with its leader, used as the brand glyph. */
export function Logo({ size = 22, word = true }: { size?: number; word?: boolean }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 9 }}>
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
        <circle cx="15" cy="9" r="6.2" stroke="currentColor" strokeWidth="1.5" />
        <circle cx="15" cy="9" r="1.8" fill="var(--signal)" />
        <path d="M10.6 13.4 3 21H8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      {word && (
        <span style={{ font: "600 15px/1 var(--font-ui)", letterSpacing: "-0.01em" }}>
          Trust<span style={{ fontWeight: 500, color: "var(--paper-2)" }}>PCB</span>
        </span>
      )}
    </span>
  );
}

export function Idx({ children, style }: { children: ReactNode; style?: CSSProperties }) {
  return <span className="idx" style={style}>{children}</span>;
}

export function Panel({
  title, actions, children, className = "", style, bodyClass, bodyStyle,
}: {
  title?: string; actions?: ReactNode; children?: ReactNode; className?: string; style?: CSSProperties;
  bodyClass?: string; bodyStyle?: CSSProperties;
}) {
  return (
    <section className={`panel ${className}`} style={style}>
      {(title || actions) && (
        <header className="panel-head">
          {title && <h2>{title}</h2>}
          <span className="grow" />
          {actions}
        </header>
      )}
      <div className={bodyClass} style={bodyStyle}>{children}</div>
    </section>
  );
}

export function SevGlyph({ sev, size = 12 }: { sev: Severity; size?: number }) {
  const c = `var(--sev-${sev})`;
  const s = size;
  return (
    <svg width={s} height={s} viewBox="0 0 12 12" aria-label={sev} role="img" style={{ flex: "none" }}>
      {sev === "critical" && (<><circle cx="6" cy="6" r="5.3" fill="none" stroke={c} strokeWidth="1" /><circle cx="6" cy="6" r="2.9" fill={c} /></>)}
      {sev === "high" && <circle cx="6" cy="6" r="4.6" fill={c} />}
      {sev === "medium" && (<><circle cx="6" cy="6" r="4.6" fill="none" stroke={c} strokeWidth="1.1" /><path d="M6 1.4a4.6 4.6 0 000 9.2z" fill={c} /></>)}
      {sev === "low" && <circle cx="6" cy="6" r="4.2" fill="none" stroke={c} strokeWidth="1.2" />}
      {sev === "info" && <circle cx="6" cy="6" r="4.2" fill="none" stroke={c} strokeWidth="1.1" strokeDasharray="1.6 1.6" />}
    </svg>
  );
}

const CLASS_LABEL: Record<EvidenceClass, string> = { measured: "Measured", simulated: "Simulated", opinion: "Engineering opinion", not_tested: "Not tested" };
export function ClassStamp({ cls }: { cls: EvidenceClass }) {
  return (
    <span className={`stamp ${cls === "simulated" ? "sim" : ""}`} title={CLASS_LABEL[cls]}>
      {cls === "simulated" && <i />}
      {cls === "measured" && <svg width="9" height="9" viewBox="0 0 9 9" fill="none" stroke="currentColor"><path d="M1 8V1M1 8h7M1 3h1.5M1 5.5h1.5M3.5 8V6.5M6 8V6.5" /></svg>}
      {cls === "opinion" && <svg width="9" height="9" viewBox="0 0 9 9" fill="none" stroke="currentColor"><path d="M1 2h7M1 4.5h7M1 7h4" /></svg>}
      {cls === "not_tested" && <svg width="9" height="9" viewBox="0 0 9 9" fill="none" stroke="currentColor" strokeDasharray="1.4 1.2"><circle cx="4.5" cy="4.5" r="3.6" /></svg>}
      {CLASS_LABEL[cls]}
    </span>
  );
}

export function FixtureBadge({ label = "Fixture", title = "Dev fixture data. Not a real review.", style }: { label?: string; title?: string; style?: CSSProperties }) {
  if (DATA_SOURCE !== "fixture") return null;
  return <span className="stamp fixture" title={title} style={style}>{label}</span>;
}

/** "Read-only demo": shown when the backend runs with TRUSTPCB_DEMO_READONLY=1. Says what is switched off and links to the repository. */
export function DemoNotice({ block = false }: { block?: boolean }) {
  const { demo, repoUrl } = useAppEnv();
  if (!demo) return null;
  const title = "Uploading, starting reviews and every other change are switched off. Everything shown here was produced by a real review.";
  const repo = repoUrl ? <a href={repoUrl} target="_blank" rel="noreferrer" className="signal">Repository</a> : null;
  if (!block) return <span className="stamp" role="note" data-testid="demo-notice" title={title}>Read-only demo{repo && <> <span aria-hidden>·</span> {repo}</>}</span>;
  return (
    <p className="warn" role="note" data-testid="demo-notice-block">
      <span className="signal mono">!</span>
      <span><b>Read-only demo.</b> Uploading and starting reviews are switched off here. {repoUrl ? <>The code is in the <a href={repoUrl} target="_blank" rel="noreferrer" className="signal">repository</a>: run it on your own board.</> : "Run TrustPCB on your own machine to review your own board."}</span>
    </p>
  );
}

export function Meter({ value, signal, style }: { value: number; signal?: boolean; style?: CSSProperties }) {
  return (
    <div className={`meter ${signal ? "sig" : ""}`} style={style} role="progressbar" aria-valuenow={Math.round(value * 100)} aria-valuemin={0} aria-valuemax={100}>
      <b style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
    </div>
  );
}

export function SevBar({ counts, size = "md" }: { counts: Record<Severity, number>; size?: "sm" | "md" }) {
  const order: Severity[] = ["critical", "high", "medium", "low", "info"];
  return (
    <span style={{ display: "inline-flex", gap: size === "sm" ? 8 : 12, alignItems: "center" }}>
      {order.map((s) => (
        <span key={s} className="mono" style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: size === "sm" ? 11 : 12, opacity: counts[s] ? 1 : 0.3 }} title={`${counts[s]} ${s}`}>
          <SevGlyph sev={s} size={size === "sm" ? 10 : 12} />
          {counts[s]}
        </span>
      ))}
    </span>
  );
}

export function TopBar({ children, right, current }: { children?: ReactNode; right?: ReactNode; current?: string }) {
  const { demo } = useAppEnv();
  return (
    <header className="topbar">
      <Link href="/" aria-label="TrustPCB home" className="topbar-brand"><Logo /></Link>
      <nav className="topnav" aria-label="Main">
        <Link className="navitem" href="/projects" aria-current={current === "projects" ? "page" : undefined}><Icon name="folder" size={17} /> Projects</Link>
        {!demo && <Link className="navitem" href="/upload" aria-current={current === "upload" ? "page" : undefined}><Icon name="upload" size={17} /> Upload a board</Link>}
        <Link className="navitem" href="/compare" aria-current={current === "compare" ? "page" : undefined}><Icon name="compare" size={17} /> Compare</Link>
      </nav>
      {children}
      <div className="topbar-foot">
        {right}
        <DemoNotice />
        <FixtureBadge />
      </div>
    </header>
  );
}

export function Sparkline({ values, w = 80, h = 22 }: { values: number[]; w?: number; h?: number }) {
  const max = Math.max(...values, 1);
  const pts = values.map((v, i) => [values.length === 1 ? w / 2 : (i / (values.length - 1)) * (w - 4) + 2, h - 3 - (v / max) * (h - 7)]);
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden>
      <path d={`M2 ${h - 1.5}H${w - 2}`} stroke="var(--hair)" />
      <polyline points={pts.map((p) => p.join(",")).join(" ")} fill="none" stroke="var(--paper)" strokeWidth="1.2" />
      {pts.map((p, i) => (<circle key={i} cx={p[0]} cy={p[1]} r={i === pts.length - 1 ? 2.4 : 1.6} fill={i === pts.length - 1 ? "var(--signal)" : "var(--stage)"} stroke={i === pts.length - 1 ? "none" : "var(--paper)"} />))}
    </svg>
  );
}
