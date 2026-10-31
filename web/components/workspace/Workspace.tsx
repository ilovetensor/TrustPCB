"use client";
import { useAppEnv } from "../../lib/demo";
import "./workspace.css";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { FIXTURE_MODE, getBoardBundle, getEvidenceByCode, getSnapshot, listRevisions, type BoardBundle } from "../../lib/api";
import { changeByCode, headlineOf, screenVsConfirm, testsOf, versionChangeOf, type ChangeState, type Headline, type StoryRound, type StoryTest, type VersionChange } from "../../lib/story.mjs";
import { indexBoard, resolveMark, bboxUnion } from "../../lib/geometry.mjs";
import { useNarrow } from "../../lib/hooks";
import { activeMarks } from "../../lib/review-reducer.mjs";
import { buildMeta, fmtClock, findingLabel } from "../../lib/review-view";
import { useReview, type Selected } from "../../lib/use-review";
import { useReviewSims } from "../../lib/use-stage-rf";
import type { BBox, Evidence, Geometry } from "../../lib/types";
import type { FocusRequest, Ghost, Insets } from "../board2d/Board2D";
import { BoardStage, describePick } from "../stage/BoardStage";
import { DemoNotice, FixtureBadge, Icon, Logo, Panel } from "../ui";
import { BudgetInstrument } from "./Budget";
import { ChatDock, type SelectedChip } from "./Chat";
import { ExperimentStrip, type Band } from "./Experiments";
import { FindingsPanel } from "./Findings";
import { PhaseTracker } from "./PhaseTracker";
import { Trail } from "./Trail";

type Tab = "trail" | "findings" | "tests" | "ask";

interface StageLayout { insets: Insets; toolbarTop: number }

/** The free stage is whatever the floating panels leave. Measure them, so the fit follows the real layout. */
function useStageLayout(root: React.RefObject<HTMLElement | null>, narrow: boolean): StageLayout {
  const [layout, setLayout] = useState<StageLayout>({ insets: { l: 312, r: 360, t: 126, b: 156 }, toolbarTop: 80 });
  useLayoutEffect(() => {
    const el = root.current;
    if (!el) return;
    const measure = () => {
      const q = (sel: string) => el.querySelector<HTMLElement>(sel);
      if (narrow) {
        // phone: the toolbar sits on the stage's top edge, the board fits below it
        const h = q(".stage-tools")?.offsetHeight ?? 0;
        const next: StageLayout = { insets: { l: 0, r: 0, t: h ? h + 16 : 0, b: 0 }, toolbarTop: 8 };
        setLayout((cur) => (JSON.stringify(cur) === JSON.stringify(next) ? cur : next));
        return;
      }
      const trail = q(".ws-trail"), find = q(".ws-find"), ex = q(".ws-ex"), tools = q(".stage-tools");
      const head = Math.max(...[".ws-brand", ".ws-phase", ".ws-budget"].map((s) => { const n = q(s); return n ? n.offsetTop + n.offsetHeight : 0; }));
      const toolbarTop = head + 12;
      const next: StageLayout = {
        insets: {
          l: trail ? trail.offsetLeft + trail.offsetWidth + 12 : 0,
          r: find ? el.clientWidth - find.offsetLeft + 12 : 0,
          t: toolbarTop + (tools?.offsetHeight ?? 36) + 10,
          b: ex ? el.clientHeight - ex.offsetTop + 10 : 0,
        },
        toolbarTop,
      };
      setLayout((cur) => (JSON.stringify(cur) === JSON.stringify(next) ? cur : next));
    };
    measure();
    const timers = [setTimeout(measure, 300), setTimeout(measure, 1200)];
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    const tools = el.querySelector(".stage-tools");
    if (tools) ro.observe(tools);
    window.addEventListener("resize", measure);
    return () => { timers.forEach(clearTimeout); ro.disconnect(); window.removeEventListener("resize", measure); };
  }, [root, narrow]);
  return layout;
}

export function Workspace({ reviewId }: { reviewId: string }) {
  const { demo } = useAppEnv();
  const sp = useSearchParams();
  const at = Number(sp.get("at") ?? 0) || 0;
  const rv = useReview(reviewId, { startAt: at, autoplay: sp.get("play") !== "0" });
  const { state, meta } = rv;
  const narrow = useNarrow();
  const root = useRef<HTMLElement>(null);
  const layout = useStageLayout(root, narrow);
  const [bundle, setBundle] = useState<BoardBundle | null>(null);
  const [boardError, setBoardError] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<Record<string, Evidence>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const [spot, setSpot] = useState<{ targets: string[]; nonce: number } | null>(null);
  const [tab, setTab] = useState<Tab>("findings");
  const [chatOpen, setChatOpen] = useState(false);
  const [seed, setSeed] = useState<string | undefined>();
  const board = bundle?.board ?? null;

  useEffect(() => {
    if (!meta) return;
    let live = true;
    getBoardBundle(meta.version_id).then((b) => { if (live) setBundle(b); }).catch((e) => { if (live) setBoardError(e instanceof Error ? e.message : "The board could not be loaded."); });
    return () => { live = false; };
  }, [meta?.version_id]); // eslint-disable-line react-hooks/exhaustive-deps

  const revision = Math.max(state.reportReady?.revision ?? 0, meta?.report_revision ?? 0);       // the latest revision: a follow-up may have written newer ones than the report_ready event
  useEffect(() => { if (meta) void getEvidenceByCode(reviewId).then(setEvidence); }, [reviewId, revision, meta?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // the report snapshot holds the partial win and the screen-against-confirm rows; there is none until the review has a report
  const [story, setStory] = useState<{ headline: Headline | null; change: VersionChange | null; changes: Record<string, { change: ChangeState; note: string | null; changes: string[] }>; rounds: StoryRound[]; tests: StoryTest[] }>({ headline: null, change: null, changes: {}, rounds: [], tests: [] });
  useEffect(() => {
    if (FIXTURE_MODE || !revision) return;
    let live = true;
    listRevisions(reviewId).then((r) => (r.length ? getSnapshot(reviewId, r[r.length - 1].number) : null)).then((snap) => { if (live && snap) setStory({ headline: headlineOf(snap), change: versionChangeOf(snap), changes: changeByCode(snap), rounds: screenVsConfirm(snap), tests: testsOf(snap) }); }).catch(() => {});
    return () => { live = false; };
  }, [reviewId, revision]);

  const index = useMemo(() => (board ? indexBoard(board) : null), [board]);
  const findingsMeta = useMemo(() => buildMeta(state.findingOrder.map((id) => state.findings[id])), [state.findingOrder, state.findings]);
  const marks = useMemo(() => activeMarks(state), [state]);
  const simsWorking = Object.values(state.sims).some((x) => x.status !== "done");
  const rfs = useReviewSims(reviewId, state.experiments.length + Object.keys(state.sims).length, simsWorking);

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
      const geoms = f ? f.marks.flatMap((m) => m.resolved ?? []) : [];
      if (geoms.length) {
        const u = bboxUnion(geoms.map((g) => g.bbox));
        if (u) setFocus({ bbox: u as BBox, nonce: Date.now() + Math.random(), maxScale: 12 });
      } else if (f) focusOn(f.marks.flatMap((m) => m.targets), 12);
      if (narrow) setTab("findings");
    }
  }, [state.findings, focusOn, narrow]);

  const ghost: Ghost | null = useMemo(() => {
    const w = state.followup.whatif;
    if (!w || !board) return null;
    const fp = board.footprints.find((f) => f.ref === w.ref);
    const to = (Array.isArray(w.new_position) ? w.new_position : w.to) as [number, number] | undefined;
    if (!fp || !to) return null;
    return { ref: w.ref, from: [fp.x, fp.y], to, size: [fp.bbox[2] - fp.bbox[0], fp.bbox[3] - fp.bbox[1]], label: w.label ?? "What-if, not your board", ring: w.ghost?.ring, pads: w.ghost?.pads };
  }, [state.followup.whatif, board]);

  // a new what-if brings the camera to the ghost part and the parts around it
  const whatifSeen = useRef<unknown>(null);
  useEffect(() => {
    if (!ghost || whatifSeen.current === state.followup.whatif) return;
    whatifSeen.current = state.followup.whatif;
    // the chat floats over the right half of the stage: bring the ghost into the left half by fitting extra board to its right
    const r = 8, room = narrow ? 0 : 34;
    setFocus({ bbox: [Math.min(ghost.from[0], ghost.to[0]) - r, Math.min(ghost.from[1], ghost.to[1]) - r, Math.max(ghost.from[0], ghost.to[0]) + r + room, Math.max(ghost.from[1], ghost.to[1]) + r] as BBox, nonce: Date.now() + Math.random(), maxScale: 9 });
  }, [ghost, state.followup.whatif, narrow]);

  const sel: SelectedChip[] = useMemo(() => {
    const out: SelectedChip[] = [];
    const f = selectedId ? state.findings[selectedId] : null;
    if (f) out.push({ id: f.code ?? f.id, label: findingLabel(f), type: "finding" });
    if (picked && board) out.push({ id: picked, label: describePick(board, picked).title, type: "object" });
    return out;
  }, [selectedId, picked, state.findings, board]);

  const askFinding = (id: string) => {
    const f = state.findings[id];
    setSelectedId(id);
    setSeed(`Why is ${f ? findingLabel(f) : "this"} a problem?`);
    setChatOpen(true);
    if (narrow) setTab("ask");
  };
  const askObject = (id: string) => {
    setPicked(id);
    setSeed(`What should I know about ${board ? describePick(board, id).title : id}?`);
    setChatOpen(true);
    if (narrow) setTab("ask");
  };
  const q = (k: string) => sp.get(k);

  const goalBand = meta?.goal?.band_hz ?? (state.brief?.wireless?.band_hz as [number, number] | undefined);
  const band: Band | null = goalBand ? { lo_ghz: goalBand[0] / 1e9, hi_ghz: goalBand[1] / 1e9, target_db: meta?.goal?.target_s11_db ?? -10 } : null;
  const rfNote = meta?.rf && meta.rf.status === "not_tested" ? `Not tested. ${meta.rf.reason ?? ""}`.trim() : null;
  const pendingApproval = Object.values(state.requests).some((r) => r.status === "pending");
  const topFinding = state.findingOrder.map((id) => state.findings[id]).find((f) => f && f.status !== "dismissed");
  const suggest = topFinding
    ? [`Why is ${findingLabel(topFinding)} a problem?`, "What did you not test?", "What should I fix first?"]
    : ["What did you not test?", "What should I check first?"];
  const running = state.status === "running" || state.status === "idle";
  const canStop = state.status === "running" || state.followup.running;
  const connection = FIXTURE_MODE ? "REPLAY" : rv.stream === "retrying" ? "RECONNECTING" : rv.stream === "error" ? "OFFLINE" : state.status === "running" ? "LIVE" : state.status === "idle" ? "LOADING" : state.endState === "interrupted" ? "INTERRUPTED" : state.status.toUpperCase();

  const brand = (
    <Panel className="ws-brand" idx={meta ? `R-${meta.id.replace(/^r-/i, "").slice(0, 6).toUpperCase()}` : "R-----"} delay={0}>
      <div className="ws-brand-in">
        <Link href="/projects" aria-label="Projects"><Logo size={20} word={false} /></Link>
        <div className="ws-brand-t">
          <b>{meta?.project_name ?? "Loading"}</b>
          <span className="idx">{meta?.version_label ?? ""} / {meta?.depth?.toUpperCase() ?? ""} / {connection}</span>
        </div>
        <FixtureBadge label="Fixture replay" />
        <DemoNotice />
      </div>
    </Panel>
  );
  const phase = (
    <Panel className="ws-phase" delay={80} crop={false}>
      <PhaseTracker phase={state.phase} status={state.status} compact={narrow} refine={state.refine} />
    </Panel>
  );
  const budget = (
    <Panel className="ws-budget" idx="BUDGET" delay={160}>
      <BudgetInstrument budget={state.budget} status={state.status} canStop={canStop} onStop={() => { void rv.stop(); }} />
    </Panel>
  );
  const trail = (
    <Panel className="ws-trail" idx="TRAIL" title="Investigation" delay={240} bodyClass="fill" actions={<span className="idx">{state.trail.length} ENTRIES</span>}>
      <Trail state={state} onSelectFinding={selectFinding} story={story.rounds} tests={story.tests} />
    </Panel>
  );
  const findings = (
    <Panel className="ws-find" idx="FINDINGS" title="Findings" delay={320} bodyClass="fill" actions={(
      <span className="ws-find-act">
        {!demo && <button className="btn sm" onClick={() => setChatOpen((o) => !o)} disabled={running && !pendingApproval} title={running ? "Chat opens when the review ends" : undefined} aria-pressed={chatOpen}>
          <Icon name="chat" size={12} /> {pendingApproval ? "Approve" : "Ask"}{pendingApproval && <i className="ws-dot" />}
        </button>}
        {revision ? <Link href={`/reviews/${reviewId}/report`} className="btn sm primary">Report r{revision}</Link> : null}
      </span>
    )}>
      <FindingsPanel headline={story.headline} versionChange={story.change} changes={story.changes} onFindingCode={(code) => { const f = state.findingOrder.map((id) => state.findings[id]).find((x) => x?.code === code); if (f) selectFinding(f.id); }} state={state} evidence={evidence} selectedId={selectedId} onSelect={selectFinding} onAsk={askFinding} onSetStatus={(id, st, reason) => { void rv.setFindingStatus(id, st, reason); if (st === "dismissed") setSelectedId(null); }} />
    </Panel>
  );
  const strip = (
    <Panel className="ws-ex" idx="EXPERIMENTS" delay={400} bodyClass="fill">
      <ExperimentStrip state={state} reviewId={reviewId} band={band} rfNote={rfNote} jobs={rfs.jobs} source={rfs.source} />
    </Panel>
  );
  const chat = (
    <Panel className="ws-chat" idx="ASK" title="Follow-up" delay={0} bodyClass="fill" actions={!narrow ? <button className="icon-btn" onClick={() => setChatOpen(false)} aria-label="Close chat"><Icon name="close" size={12} /></button> : undefined}>
      <ChatDock
        state={state} selected={sel} suggest={suggest} error={rv.error}
        onClearSel={(id) => { if (picked === id) setPicked(null); else setSelectedId(null); }}
        onSend={(t) => { void rv.send(t, sel.map((s): Selected => ({ id: s.id, type: s.type }))); setSeed(undefined); }}
        onDecide={(id, ok) => { void rv.decide(id, ok); }} busy={rv.sending} seed={seed}
      />
    </Panel>
  );
  const rp = rv.replay;
  const replay = rp && (
    <div className="ws-replay hair panel">
      <span className="stamp fixture">Fixture replay</span>
      <button className="icon-btn" onClick={() => rp.setPlaying(!rp.playing)} aria-label={rp.playing ? "Pause replay" : "Play replay"}><Icon name={rp.playing ? "pause" : "play"} size={12} /></button>
      <input type="range" min={0} max={Math.max(1, Math.round(rp.duration))} step={0.5} value={Math.min(rp.clock, rp.duration)} onChange={(e) => rp.seek(+e.target.value)} aria-label="Replay position" />
      <span className="mono ws-clock">{fmtClock(rp.clock)}</span>
      <div className="seg">{[1, 3, 8].map((s) => (<button key={s} aria-pressed={rp.speed === s} onClick={() => rp.setSpeed(s)}>{s}x</button>))}</div>
      <button className="icon-btn" onClick={() => { setSelectedId(null); rp.restart(); }} aria-label="Restart replay"><Icon name="rotate" size={12} /></button>
    </div>
  );

  const stage = board ? (
    <BoardStage
      board={board} glbUrl={bundle?.glbUrl ?? null} scene={bundle?.scene ?? null} zones={bundle?.zones ?? []}
      marks={marks} meta={findingsMeta} selectedId={selectedId} onSelect={selectFinding}
      insets={layout.insets} toolbarTop={layout.toolbarTop} focus={focus} spotlight={spot} rf={rfs.rf} ghost={ghost} pickedId={picked} onPick={setPicked} onAsk={askObject}
      initialMode={q("mode") === "3d" ? "3d" : "2d"} initialExplode={Number(q("explode") ?? 0) || 0} rfAvailable={!!rfs.rf}
    />
  ) : (
    <div className="stage"><div className="stage-bg" aria-hidden /><div className="stage-loading mono idx">{(boardError ?? (!meta ? rv.error : null))?.toUpperCase() ?? "LOADING BOARD"}</div></div>
  );

  const banner = state.endState === "interrupted"
    ? "This review was interrupted because the app restarted while it was running. Nothing after that point was done. Start a new review of this version to get a full report."
    : state.status === "failed"
    ? `The review failed${state.failure ? `: ${state.failure}` : "."}`
    : state.status === "canceled" ? "The review was stopped. The report lists what was not done." : rv.stream === "retrying" ? "Connection to the local service dropped. Reconnecting." : null;

  if (narrow) {
    return (
      <main className="ws narrow" ref={root}>
        <div className="ws-narrow-head">{brand}{budget}</div>
        {phase}
        <div className="ws-stage">{stage}{replay}</div>
        <div className="ws-tabs seg" role="tablist">
          {(["trail", "findings", "tests", "ask"] as Tab[]).map((t) => (<button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>{t === "tests" ? "Tests" : t}{t === "findings" && state.findingOrder.length ? ` ${state.findingOrder.length}` : ""}{t === "ask" && pendingApproval ? " !" : ""}</button>))}
        </div>
        {banner && <div className="ws-banner">{banner}</div>}
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
    <main className="ws wide" ref={root}>
      <div className="ws-stage">{stage}</div>
      {brand}{phase}{budget}{trail}{findings}{strip}
      {replay}
      {chatOpen && !demo && chat}
      {banner && <div className="ws-notice stamp sig">{banner}</div>}
      {!banner && state.notices.slice(-1).map((n) => (<div key={n.id} className="ws-notice stamp sig">{n.text}</div>))}
    </main>
  );
}
