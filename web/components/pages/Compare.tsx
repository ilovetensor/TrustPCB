"use client";
import "./pages.css";
import "./compare.css";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { FIXTURE_MODE, getBoardBundle, getCompare, getProject, type BoardBundle, type CompareResult, type FindingBrief } from "../../lib/api";
import { bboxUnion } from "../../lib/geometry.mjs";
import type { BBox, Mark, Severity } from "../../lib/types";
import { Board2D, type ActiveMarkItem } from "../board2d/Board2D";
import { useScene } from "../board2d/useScene";
import { DEFAULT_LAYERS } from "../board2d/layers";
import { CompareFixture } from "./CompareFixture";
import { Icon, Panel, SevBar, SevGlyph, TopBar } from "../ui";

const none = { l: 0, r: 0, t: 0, b: 0 };
const GROUPS: { key: keyof NonNullable<CompareResult["findings"]>; label: string }[] = [
  { key: "new", label: "NEW" }, { key: "fixed", label: "FIXED" }, { key: "changed", label: "CHANGED" },
  { key: "still_present", label: "STILL PRESENT" }, { key: "dismissed_carried", label: "DISMISSED, CARRIED" }, { key: "not_rechecked", label: "NOT RECHECKED" },
];

const asMarks = (marks: Mark[] | undefined, severity: Severity, prefix: string): ActiveMarkItem[] =>
  (marks ?? []).map((m, i) => ({ key: `${prefix}:${i}`, findingId: null, severity, mark: m }));

function CompareLive() {
  const sp = useSearchParams();
  const [from, setFrom] = useState<string | null>(sp.get("from"));
  const [to, setTo] = useState<string | null>(sp.get("to"));
  const projectParam = sp.get("project");
  const [cmp, setCmp] = useState<CompareResult | null>(null);
  const [a, setA] = useState<BoardBundle | null>(null);
  const [b, setB] = useState<BoardBundle | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [split, setSplit] = useState(50);
  const [view, setView] = useState<{ scale: number; tx: number; ty: number } | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef(false);

  // /compare?project=<id> picks the last two versions that have a review
  useEffect(() => {
    if (from && to) return;
    if (!projectParam) { setError("Pick two versions from a project page: Compare versions."); return; }
    void getProject(projectParam).then((p) => {
      const vs = (p?.versions ?? []).filter((v) => v.review_id);
      if (vs.length < 2) { setError("This project needs two reviewed versions to compare."); return; }
      setFrom(vs[vs.length - 2].id); setTo(vs[vs.length - 1].id);
    }).catch((e) => setError(e instanceof Error ? e.message : "Could not load the project."));
  }, [from, to, projectParam]);

  useEffect(() => {
    if (!from || !to) return;
    setError(null);
    void Promise.all([getCompare(from, to), getBoardBundle(from), getBoardBundle(to)])
      .then(([c, x, y]) => { setCmp(c); setA(x); setB(y); })
      .catch((e) => setError(e instanceof Error ? e.message : "The comparison failed."));
  }, [from, to]);

  const sceneA = useScene(a?.scene), sceneB = useScene(b?.scene);
  const marksA = useMemo(() => asMarks(cmp?.board ? (cmp.board as unknown as { marks?: { from: Mark[] } }).marks?.from : undefined, "low", "a"), [cmp]);
  const marksB = useMemo(() => asMarks(cmp?.board ? (cmp.board as unknown as { marks?: { to: Mark[] } }).marks?.to : undefined, "medium", "b"), [cmp]);
  const fit = useMemo<BBox | null>(() => (a && b ? (bboxUnion([a.board.bbox, b.board.bbox]) as BBox) : null), [a, b]);

  const move = (e: React.PointerEvent) => {
    if (!drag.current || !box.current) return;
    const r = box.current.getBoundingClientRect();
    setSplit(Math.min(96, Math.max(4, ((e.clientX - r.left) / r.width) * 100)));
  };
  const diff = cmp?.board;
  const nA = cmp?.from.number, nB = cmp?.to.number;

  return (
    <div className="pg">
      <TopBar current="compare" />
      <main className="pg-main" style={{ maxWidth: 1440 }}>
        <div className="pg-head">
          <div>
            <span className="idx">COMPARE {cmp ? `/ V${nA} TO V${nB}` : ""}</span>
            <h1 className="serif">What changed between versions.</h1>
          </div>
          <span className="grow" />
          {cmp && <div className="seg" aria-label="Versions"><button aria-pressed>A: v{nA}</button><button aria-pressed>B: v{nB}</button></div>}
        </div>
        {error && <p className="chat-err" role="alert">{error}</p>}
        {!error && !cmp && <p className="dim">Comparing.</p>}
        {cmp && (
          <div className="cmp">
            <div className="cmp-board hair panel" ref={box} onPointerMove={move} onPointerUp={() => { drag.current = false; }} onPointerLeave={() => { drag.current = false; }}>
              {a && b && (
                <>
                  <div className="cmp-layer"><Board2D board={a.board} marks={marksA} meta={{}} selectedId={null} onSelect={() => {}} layers={DEFAULT_LAYERS} insets={none} interactive={false} chrome={false} externalView={view} onViewChange={setView} fitTo={fit} scene={sceneA} /></div>
                  <div className="cmp-layer top" style={{ clipPath: `inset(0 0 0 ${split}%)` }}><Board2D board={b.board} marks={marksB} meta={{}} selectedId={null} onSelect={() => {}} layers={DEFAULT_LAYERS} insets={none} onViewChange={setView} externalView={view} fitTo={fit} scene={sceneB} chrome={false} /></div>
                  <div className="cmp-handle" style={{ left: `${split}%` }} onPointerDown={(e) => { drag.current = true; (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); }}>
                    <span /><b><Icon name="compare" size={14} /></b>
                  </div>
                  <span className="cmp-lab a stamp solid">A / v{nA}</span>
                  <span className="cmp-lab b stamp solid">B / v{nB}</span>
                </>
              )}
            </div>

            <div className="cmp-side">
              <Panel className="card" idx="COUNTS" title="Open findings" delay={100}>
                <div style={{ padding: 14, display: "grid", gap: 10 }}>
                  <div style={{ display: "flex", gap: 12, alignItems: "center" }}><span className="idx">A v{nA}</span>{cmp.from.counts ? <SevBar counts={cmp.from.counts} size="sm" /> : <span className="dim">no review</span>}</div>
                  <div style={{ display: "flex", gap: 12, alignItems: "center" }}><span className="idx">B v{nB}</span>{cmp.to.counts ? <SevBar counts={cmp.to.counts} size="sm" /> : <span className="dim">no review</span>}</div>
                  <p className="idx">RESONANCE: NOT TESTED. THE COMPARISON HOLDS NO SIMULATION RESULTS.</p>
                </div>
              </Panel>
              <Panel className="card" idx="CHANGES" title="Findings" delay={200}>
                {cmp.findings ? (
                  <>
                    {GROUPS.map((g) => {
                      const rows = cmp.findings![g.key] as FindingBrief[];
                      if (!rows.length) return null;
                      return (
                        <div key={g.key}>
                          <div className="idx" style={{ padding: "10px 14px 4px" }}>{g.label} ({rows.length})</div>
                          <ul className="cmp-list">
                            {rows.map((r) => (
                              <li key={r.id} className={g.key}>
                                <span className="idx">{r.code}</span>
                                <span className="cmp-t">{r.title}</span>
                                <span className="cmp-s"><SevGlyph sev={(r.was?.severity ?? r.severity) as Severity} size={10} />{r.was && r.was.severity !== r.severity && <><i>&rarr;</i><SevGlyph sev={r.severity} size={10} /></>}</span>
                                {r.changes && r.changes.length > 0 && <span className="dim cmp-v">{r.changes.join("; ")}</span>}
                                {r.dismissed_reason && <span className="dim cmp-v">Dismissed: {r.dismissed_reason}</span>}
                              </li>
                            ))}
                          </ul>
                        </div>
                      );
                    })}
                    {GROUPS.every((g) => !(cmp.findings![g.key] as FindingBrief[]).length) && <p className="empty">Neither version has findings.</p>}
                  </>
                ) : <p className="empty">Both versions need a finished review to compare findings.</p>}
              </Panel>
              <Panel className="card" idx="BOARD" title="Board differences" delay={300}>
                {diff ? (
                  <ul className="cmp-list" style={{ padding: "6px 14px 14px" }}>
                    <li><span className="idx">PARTS</span><span className="cmp-t">{diff.parts.added.length} added, {diff.parts.removed.length} removed, {diff.parts.moved.length} moved, {diff.parts.value_changed.length} value changed</span></li>
                    {diff.parts.moved.slice(0, 5).map((m) => (<li key={m.ref}><span className="idx">{m.ref}</span><span className="cmp-t">moved {m.distance_mm} mm</span></li>))}
                    {diff.parts.value_changed.slice(0, 5).map((m) => (<li key={m.ref}><span className="idx">{m.ref}</span><span className="cmp-t">{m.from} to {m.to}</span></li>))}
                    <li><span className="idx">TRACKS</span><span className="cmp-t">+{diff.tracks.added} / -{diff.tracks.removed} ({diff.tracks.length_added_mm} / {diff.tracks.length_removed_mm} mm)</span></li>
                    <li><span className="idx">VIAS</span><span className="cmp-t">{diff.vias.count_old} to {diff.vias.count_new}</span></li>
                    <li><span className="idx">ZONES</span><span className="cmp-t">{diff.zones.old} to {diff.zones.new}</span></li>
                    {diff.nets.added.length + diff.nets.removed.length > 0 && <li><span className="idx">NETS</span><span className="cmp-t">+{diff.nets.added.join(", ") || "none"} / -{diff.nets.removed.join(", ") || "none"}</span></li>}
                  </ul>
                ) : <p className="empty">Board differences are not available.</p>}
                {cmp.notes.length > 0 && <p className="dim" style={{ fontSize: 12, padding: "0 14px 14px", margin: 0 }}>{cmp.notes.join(" ")}</p>}
                <p className="dim" style={{ fontSize: 12, padding: "0 14px 14px", margin: 0 }}>Blue marks on A are removed copper and parts. Amber marks on B are added or moved ones. Dismissals carry forward by fingerprint.</p>
              </Panel>
              {cmp.to.review_id && <Link className="btn" href={`/reviews/${cmp.to.review_id}`}>Open v{nB} workspace <Icon name="arrow" size={13} /></Link>}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export function Compare() {
  if (FIXTURE_MODE) return <CompareFixture />;
  return (<Suspense fallback={null}><CompareLive /></Suspense>);
}
