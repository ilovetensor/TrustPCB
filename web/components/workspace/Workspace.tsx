"use client";
import { useAppEnv } from "../../lib/demo";
import "./workspace.css";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FIXTURE_MODE, getBoardBundle, getEvidenceByCode, getSnapshot, listRevisions, type BoardBundle } from "../../lib/api";
import { changeByCode, headlineOf, screenVsConfirm, testsOf, versionChangeOf, type ChangeState, type Headline, type StoryRound, type StoryTest, type VersionChange } from "../../lib/story.mjs";
import { indexBoard, resolveMark, bboxUnion } from "../../lib/geometry.mjs";
import { tabProps, tablistKeys, useNarrow } from "../../lib/hooks";
import { activeMarks } from "../../lib/review-reducer.mjs";
import { buildMeta, fmtClock, findingLabel } from "../../lib/review-view";
import { useReview, type Selected } from "../../lib/use-review";
import { useReviewSims } from "../../lib/use-stage-rf";
import type { BBox, Evidence, Geometry } from "../../lib/types";
import type { FocusRequest, Ghost, Insets } from "../board2d/Board2D";
import { BoardStage, describePick } from "../stage/BoardStage";
import { DemoNotice, Icon } from "../ui";
import { PartialWin } from "../rf/PartialWin";
import { VersionChange as VersionChangeCard } from "../rf/VersionChange";
import { ChatDock, type SelectedChip } from "./Chat";
import { Experiments, type Band } from "./Experiments";
import { FindingsPanel } from "./Findings";
import { RunProgress } from "./RunProgress";
import { Sidebar, VIEWS, type NavEntry, type ViewId } from "./Sidebar";
import { Trail } from "./Trail";

const WIDE_INSETS: Insets = { l: 0, r: 0, t: 40, b: 40 };
const NARROW_INSETS: Insets = { l: 0, r: 0, t: 40, b: 0 };
const cap = (t: string) => t.charAt(0).toUpperCase() + t.slice(1);

export function Workspace({ reviewId }: { reviewId: string }) {
  const { demo } = useAppEnv();
  const sp = useSearchParams();
  const at = Number(sp.get("at") ?? 0) || 0;
  const rv = useReview(reviewId, { startAt: at, autoplay: sp.get("play") !== "0" });
  const { state, meta } = rv;
  const narrow = useNarrow();
  const [bundle, setBundle] = useState<BoardBundle | null>(null);
  const [boardError, setBoardError] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<Record<string, Evidence>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const [spot, setSpot] = useState<{ targets: string[]; nonce: number } | null>(null);
  const [view, setView] = useState<ViewId>("findings");
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
      setView((v) => (v === "chat" ? v : "findings"));
    }
  }, [state.findings, focusOn]);

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
    const r = 8;
    setFocus({ bbox: [Math.min(ghost.from[0], ghost.to[0]) - r, Math.min(ghost.from[1], ghost.to[1]) - r, Math.max(ghost.from[0], ghost.to[0]) + r, Math.max(ghost.from[1], ghost.to[1]) + r] as BBox, nonce: Date.now() + Math.random(), maxScale: 9 });
  }, [ghost, state.followup.whatif]);

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
    setView("chat");
  };
  const askObject = (id: string) => {
    setPicked(id);
    setSeed(`What should I know about ${board ? describePick(board, id).title : id}?`);
    setView("chat");
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
  const running = state.status === "running";
  const canStop = state.status === "running" || state.followup.running;
  const status = FIXTURE_MODE ? "Replay" : rv.stream === "retrying" ? "Reconnecting" : rv.stream === "error" ? "Offline" : state.status === "running" ? "Running" : state.status === "idle" ? "Loading" : state.endState === "interrupted" ? "Interrupted" : state.status === "canceled" ? "Stopped" : cap(state.status);
  const liveFindings = state.findingOrder.filter((id) => { const f = state.findings[id]; return f && f.status !== "dismissed" && f.status !== "superseded"; }).length;
  const hasResult = !!(story.headline || story.change);
  const shown: ViewId = view === "result" && !hasResult ? "findings" : view;
  const nav: NavEntry[] = [
    { id: "findings", count: liveFindings || null },
    ...(hasResult ? [{ id: "result" as const }] : []),
    { id: "investigation" },
    { id: "sims", count: state.experiments.length || null },
    { id: "chat", dot: pendingApproval },
  ];
  const goCode = (code: string) => { const f = state.findingOrder.map((id) => state.findings[id]).find((x) => x?.code === code); if (f) selectFinding(f.id); };
  const progress = running ? <RunProgress phase={state.phase} budget={state.budget} budgetAt={state.budgetAt} solving={simsWorking} refine={state.refine} canStop={canStop} onStop={() => { void rv.stop(); }} /> : null;

  const body = (
    <>
      {shown === "findings" && (
        <FindingsPanel
          changes={story.changes} state={state} evidence={evidence} selectedId={selectedId} onSelect={selectFinding}
          onAsk={demo ? undefined : askFinding}
          onSetStatus={(id, st, reason) => { void rv.setFindingStatus(id, st, reason); if (st === "dismissed") setSelectedId(null); }}
        />
      )}
      {shown === "result" && (
        <div className="ws-scroll ws-result">
          {story.change && <VersionChangeCard vc={story.change} onFinding={goCode} />}
          {story.headline && <PartialWin h={story.headline} onFinding={goCode} />}
        </div>
      )}
      {shown === "investigation" && <Trail state={state} onSelectFinding={selectFinding} story={story.rounds} tests={story.tests} />}
      {shown === "sims" && (
        <div className="ws-scroll">
          <Experiments state={state} reviewId={reviewId} band={band} rfNote={rfNote} jobs={rfs.jobs} source={rfs.source} />
        </div>
      )}
      {shown === "chat" && (
        <ChatDock
          state={state} selected={sel} suggest={suggest} error={rv.error}
          onClearSel={(id) => { if (picked === id) setPicked(null); else setSelectedId(null); }}
          onSend={(t) => { void rv.send(t, sel.map((s): Selected => ({ id: s.id, type: s.type }))); setSeed(undefined); }}
          onDecide={(id, ok) => { void rv.decide(id, ok); }} busy={rv.sending} seed={seed}
        />
      )}
    </>
  );
  const panel = (
    <section className="ws-panel" id="ws-panel" role="tabpanel" aria-labelledby={`ws-tab-${shown}`}>
      <header className="ws-ph">
        <Icon name={VIEWS[shown].icon} size={18} />
        <h2>{VIEWS[shown].title}</h2>
        {shown === "investigation" && state.trail.length > 0 && <span className="idx">{state.trail.length} entries</span>}
      </header>
      {body}
    </section>
  );

  const rp = rv.replay;
  const replay = rp && (
    <div className="ws-replay">
      <span className="stamp fixture">Fixture replay</span>
      <button className="icon-btn" onClick={() => rp.setPlaying(!rp.playing)} aria-label={rp.playing ? "Pause replay" : "Play replay"}><Icon name={rp.playing ? "pause" : "play"} size={13} /></button>
      <input type="range" min={0} max={Math.max(1, Math.round(rp.duration))} step={0.5} value={Math.min(rp.clock, rp.duration)} onChange={(e) => rp.seek(+e.target.value)} aria-label="Replay position" />
      <span className="ws-clock">{fmtClock(rp.clock)}</span>
      <div className="seg">{[1, 3, 8].map((x) => (<button key={x} aria-pressed={rp.speed === x} onClick={() => rp.setSpeed(x)}>{x}x</button>))}</div>
      <button className="icon-btn" onClick={() => { setSelectedId(null); rp.restart(); }} aria-label="Restart replay"><Icon name="rotate" size={13} /></button>
    </div>
  );

  const stage = board ? (
    <BoardStage
      board={board} glbUrl={bundle?.glbUrl ?? null} scene={bundle?.scene ?? null} zones={bundle?.zones ?? []}
      marks={marks} meta={findingsMeta} selectedId={selectedId} onSelect={selectFinding}
      insets={narrow ? NARROW_INSETS : WIDE_INSETS} focus={focus} spotlight={spot} rf={rfs.rf} ghost={ghost} pickedId={picked} onPick={setPicked} onAsk={demo ? undefined : askObject}
      initialMode={q("mode") === "3d" ? "3d" : "2d"} initialExplode={Number(q("explode") ?? 0) || 0} rfAvailable={!!rfs.rf}
    />
  ) : (
    <div className="stage"><div className="stage-bg" aria-hidden /><div className="stage-loading idx">{boardError ?? (!meta ? rv.error : null) ?? "Loading the board"}</div></div>
  );

  const banner = state.endState === "interrupted"
    ? "This review was interrupted because the app restarted while it was running. Nothing after that point was done. Start a new review of this version to get a full report."
    : state.status === "failed"
    ? `The review failed${state.failure ? `: ${state.failure}` : "."}`
    : state.status === "canceled" ? "The review was stopped. The report lists what was not done." : rv.stream === "retrying" ? "Connection to the local service dropped. Reconnecting." : null;
  const notice = banner ?? state.notices.slice(-1)[0]?.text ?? null;

  const projectName = meta?.project_name ?? "Loading";
  const summary = meta ? `${meta.version_label}, ${cap(meta.depth ?? "")} review`.replace(", review", "") : "";

  if (narrow) {
    return (
      <main id="main" tabIndex={-1} className="ws narrow">
        <header className="ws-narrow-head">
          <Link className="navitem" href={meta ? `/projects/${meta.project_id}` : "/projects"}><Icon name="back" size={17} /><span className="ws-ellip">{projectName}</span></Link>
          <p>{meta?.version_label ? `${meta.version_label}, ` : ""}{status.toLowerCase()}</p>
          <div className="ws-narrow-links">
            <Link className="btn sm ghost" href={`/reviews/${reviewId}/rf`}><Icon name="wave" size={13} /> RF lab</Link>
            {revision > 0 && <Link className="btn sm ghost" href={`/reviews/${reviewId}/report`}><Icon name="file" size={13} /> Report r{revision}</Link>}
            <DemoNotice />
          </div>
        </header>
        {progress}
        <div className="ws-stage">{stage}{replay}</div>
        <div className="pills ws-tabs" role="tablist" aria-label="Review views" onKeyDown={tablistKeys()}>
          {nav.map((n) => (
            <button key={n.id} id={`ws-tab-${n.id}`} {...tabProps(shown === n.id)} aria-controls="ws-panel" onClick={() => setView(n.id)}>
              <Icon name={VIEWS[n.id].icon} size={14} /> {VIEWS[n.id].title}{n.count != null ? ` ${n.count}` : ""}{n.dot && <i className="ws-dot" aria-label="Waiting for your approval" />}
            </button>
          ))}
        </div>
        {notice && <div className="ws-banner">{notice}</div>}
        <div className="ws-tabpane">{panel}</div>
      </main>
    );
  }
  return (
    <main id="main" tabIndex={-1} className="ws wide">
      <Sidebar
        projectId={meta?.project_id} projectName={projectName} summary={summary} status={status} nav={nav} view={shown} onView={setView}
        reviewId={reviewId} revision={revision} progress={progress}
      />
      <div className="ws-stage">
        {stage}
        {replay}
        {notice && <div className="ws-notice" role="status">{notice}</div>}
      </div>
      {panel}
    </main>
  );
}
