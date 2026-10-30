"use client";
import "./workspace.css";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getBoard, getBoardGlbUrl, getReview, type ReviewMeta } from "../../lib/api";
import { indexBoard, resolveMark, bboxUnion } from "../../lib/geometry.mjs";
import { useNarrow } from "../../lib/hooks";
import { activeMarks } from "../../lib/review-reducer.mjs";
import { buildMeta, fmtClock, findingLabel } from "../../lib/review-view";
import { useReview } from "../../lib/use-review";
import type { BBox, Board, Geometry } from "../../lib/types";
import type { FocusRequest, Ghost } from "../board2d/Board2D";
import { BoardStage } from "../stage/BoardStage";
import { FixtureBadge, Icon, Logo, Panel } from "../ui";
import { BudgetInstrument } from "./Budget";
import { ChatDock } from "./Chat";
import { ExperimentStrip } from "./Experiments";
import { FindingsPanel } from "./Findings";
import { PhaseTracker } from "./PhaseTracker";
import { Trail } from "./Trail";

type Tab = "trail" | "findings" | "tests" | "ask";

export function Workspace({ reviewId }: { reviewId: string }) {
  const sp = useSearchParams();
  const router = useRouter();
  const at = Number(sp.get("at") ?? 0) || 0;
  const rv = useReview(reviewId, { startAt: at, autoplay: sp.get("play") !== "0" });
  const { state } = rv;
  const narrow = useNarrow();
  const [board, setBoard] = useState<Board | null>(null);
  const [meta, setMeta] = useState<ReviewMeta | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const [spot, setSpot] = useState<{ targets: string[]; nonce: number } | null>(null);
  const [tab, setTab] = useState<Tab>("findings");
  const [chatOpen, setChatOpen] = useState(false);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [seed, setSeed] = useState<string | undefined>();

  useEffect(() => {
    getBoard().then(setBoard);
    getReview(reviewId).then(setMeta);
  }, [reviewId]);

  const index = useMemo(() => (board ? indexBoard(board) : null), [board]);
  const findingsMeta = useMemo(() => buildMeta(state.findingOrder.map((id) => state.findings[id])), [state.findingOrder, state.findings]);
  const marks = useMemo(() => activeMarks(state).filter((m) => !m.findingId || !dismissed.has(m.findingId)), [state, dismissed]);

  const focusOn = useCallback((targets: string[], maxScale?: number) => {
    if (!index) return;
    const geoms = targets.flatMap((t) => resolveMark(index, { targets: [t] }) as Geometry[]);
    const u = bboxUnion(geoms.map((g) => g.bbox));
    if (u) setFocus({ bbox: u as BBox, nonce: Date.now() + Math.random(), maxScale });
  }, [index]);

  // the agent's spotlight moves the camera to the evidence
  useEffect(() => {
    const s = state.spotlight;
    if (!s || !index) return;
    setSpot({ targets: s.targets, nonce: s.seq });
    focusOn(s.targets, 11);
    if (s.findingId) setSelectedId(s.findingId);
  }, [state.spotlight, index, focusOn]);

  const selectFinding = useCallback((id: string | null) => {
    setSelectedId(id);
    if (id) {
      const f = state.findings[id];
      if (f) focusOn(f.marks.flatMap((m) => m.targets), 12);
      if (narrow) setTab("findings");
    }
  }, [state.findings, focusOn, narrow]);

  const ghost: Ghost | null = useMemo(() => {
    const w = state.followup.whatif;
    if (!w || !board) return null;
    const fp = board.footprints.find((f) => f.ref === w.ref);
    if (!fp) return null;
    return { ref: w.ref, from: [fp.x, fp.y], to: w.to, size: [fp.bbox[2] - fp.bbox[0], fp.bbox[3] - fp.bbox[1]], label: w.label };
  }, [state.followup.whatif, board]);

  const insets = narrow ? { l: 0, r: 0, t: 0, b: 0 } : { l: 312, r: 360, t: 118, b: 148 };
  const sel = [...(selectedId ? [selectedId] : []), ...(picked ? [picked] : [])];
  const askAbout = (id: string) => { setSelectedId(id); setSeed(`Why is ${findingLabel(id)} a problem?`); setChatOpen(true); if (narrow) setTab("ask"); };
  const stop = () => { rv.setPlaying(false); };
  const q = (k: string) => sp.get(k);

  const brand = (
    <Panel className="ws-brand" idx={meta ? meta.id.toUpperCase() : "R-----"} delay={0}>
      <div className="ws-brand-in">
        <Link href="/projects" aria-label="Projects"><Logo size={20} word={false} /></Link>
        <div className="ws-brand-t">
          <b>{meta?.project_name ?? "Loading"}</b>
          <span className="idx">{meta?.version_label ?? ""} / {meta?.depth ?? ""} / {state.status === "running" ? "LIVE" : state.status === "complete" ? "COMPLETE" : state.status.toUpperCase()}</span>
        </div>
        <FixtureBadge label="Fixture replay" />
      </div>
    </Panel>
  );
  const phase = (
    <Panel className="ws-phase" delay={80} crop={false}>
      <PhaseTracker phase={state.phase} status={state.status} compact={narrow} />
    </Panel>
  );
  const budget = (
    <Panel className="ws-budget" idx="BUDGET" delay={160}>
      <BudgetInstrument budget={state.budget} status={state.status} onStop={stop} />
    </Panel>
  );

  const trail = (
    <Panel className="ws-trail" idx="TRAIL" title="Investigation" delay={240} bodyClass="fill" actions={<span className="idx">{state.trail.length} ENTRIES</span>}>
      <Trail state={state} onSelectFinding={selectFinding} />
    </Panel>
  );
  const findings = (
    <Panel className="ws-find" idx="FINDINGS" title="Findings" delay={320} bodyClass="fill" actions={state.reportReady ? (<Link href={`/reviews/${reviewId}/report`} className="btn sm primary">Report r{state.reportReady.revision}</Link>) : undefined}>
      <FindingsPanel state={state} selectedId={selectedId} onSelect={selectFinding} onAsk={askAbout} dismissed={dismissed} onDismiss={(id) => { setDismissed((d) => new Set(d).add(id)); setSelectedId(null); }} />
    </Panel>
  );
  const strip = (
    <Panel className="ws-ex" idx="EXPERIMENTS" delay={400} bodyClass="fill">
      <ExperimentStrip state={state} reviewId={reviewId} />
    </Panel>
  );
  const chat = (
    <Panel className="ws-chat" idx="ASK" title="Follow-up" delay={0} bodyClass="fill" actions={!narrow ? <button className="icon-btn" onClick={() => setChatOpen(false)} aria-label="Close chat"><Icon name="close" size={12} /></button> : undefined}>
      <ChatDock
        state={state} selected={sel} onClearSel={(s) => { if (s === selectedId) setSelectedId(null); if (s === picked) setPicked(null); }}
        onSend={(t) => { rv.send(t, sel); setSeed(undefined); }} onDecide={rv.decide} busy={false} seed={seed}
      />
    </Panel>
  );
  const replay = (
    <div className="ws-replay hair panel">
      <span className="stamp fixture">Fixture replay</span>
      <button className="icon-btn" onClick={() => rv.setPlaying(!rv.playing)} aria-label={rv.playing ? "Pause replay" : "Play replay"}><Icon name={rv.playing ? "pause" : "play"} size={12} /></button>
      <input type="range" min={0} max={Math.max(1, Math.round(rv.duration))} step={0.5} value={Math.min(rv.clock, rv.duration)} onChange={(e) => rv.seek(+e.target.value)} aria-label="Replay position" />
      <span className="mono ws-clock">{fmtClock(rv.clock)}</span>
      <div className="seg">{[1, 3, 8].map((s) => (<button key={s} aria-pressed={rv.speed === s} onClick={() => rv.setSpeed(s)}>{s}x</button>))}</div>
      <button className="icon-btn" onClick={() => { setSelectedId(null); rv.restart(); }} aria-label="Restart replay"><Icon name="rotate" size={12} /></button>
    </div>
  );

  const stage = board && (
    <BoardStage
      board={board} glbUrl={getBoardGlbUrl()} marks={marks} meta={findingsMeta} selectedId={selectedId} onSelect={selectFinding}
      insets={insets} focus={focus} spotlight={spot} rf={null} ghost={ghost} pickedId={picked} onPick={setPicked}
      initialMode={q("mode") === "3d" ? "3d" : "2d"} initialExplode={Number(q("explode") ?? 0) || 0} rfAvailable={false}
    />
  );

  if (narrow) {
    return (
      <main className="ws narrow">
        <div className="ws-narrow-head">{brand}{budget}</div>
        {phase}
        <div className="ws-stage">{stage}{replay}</div>
        <div className="ws-tabs seg" role="tablist">
          {(["trail", "findings", "tests", "ask"] as Tab[]).map((t) => (<button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>{t === "tests" ? "Tests" : t}{t === "findings" && state.findingOrder.length ? ` ${state.findingOrder.length}` : ""}</button>))}
        </div>
        <div className="ws-tabpane">
          {tab === "trail" && trail}
          {tab === "findings" && findings}
          {tab === "tests" && strip}
          {tab === "ask" && chat}
        </div>
      </main>
    );
  }
  return (
    <main className="ws wide">
      <div className="ws-stage">{stage}</div>
      {brand}{phase}{budget}{trail}{findings}{strip}
      {replay}
      {chatOpen ? chat : (
        <button className="ws-ask btn" onClick={() => setChatOpen(true)}><Icon name="chat" size={13} /> Ask about this board</button>
      )}
      {state.notices.slice(-1).map((n) => (<div key={n.id} className="ws-notice stamp sig">{n.text}</div>))}
      <button className="sr" onClick={() => router.push("/projects")}>Projects</button>
    </main>
  );
}
