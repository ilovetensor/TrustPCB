"use client";
import "./pages.css";
import "./compare.css";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { getBoardBundle, getCompare, getProject, type BoardBundle, type CompareResult, type FindingBrief } from "../../lib/api";
import { bboxUnion } from "../../lib/geometry.mjs";
import type { BBox, Mark, Severity } from "../../lib/types";
import { Board2D, type ActiveMarkItem } from "../board2d/Board2D";
import { useScene } from "../board2d/useScene";
import { DEFAULT_LAYERS } from "../board2d/layers";
import dynamic from "next/dynamic";

// The fixture compare page is a design aid. The flag is read in this file so the build can drop it from production.
const CompareFixture = process.env.NEXT_PUBLIC_TRUSTPCB_FIXTURES === "1" ? dynamic(() => import("./CompareFixture").then((m) => m.CompareFixture)) : null;
import { VersionChange } from "../rf/VersionChange";
import { CompareHeadline, CompareTuning, PatternLayer, useCompareSims, PATTERN_VIEWS, type PatternViewKey } from "./CompareRf";
import { FieldCanvas2D } from "../rf/FieldCanvas2D";
import { bestRun, useRunData } from "../../lib/use-run-data";
import { describeFreq, isScreen } from "../../lib/sims";
import { Icon, Panel, SevBar, SevGlyph, TopBar } from "../ui";

const none = { l: 0, r: 0, t: 0, b: 0 };
/** Every finding of the newer version has a `change` against the older one. The server's buckets (changed, still_present, ...) are regrouped by it. */
const GROUPS: { key: string; label: string }[] = [
  { key: "improved", label: "Improved" }, { key: "worse", label: "Worse" }, { key: "new", label: "New" }, { key: "fixed", label: "Fixed" },
  { key: "still_present", label: "Still present" }, { key: "dismissed_carried", label: "Dismissed, carried" }, { key: "not_rechecked", label: "Not rechecked" },
];
const groupOf = (f: NonNullable<CompareResult["findings"]>, key: string): FindingBrief[] => {
  if (key === "dismissed_carried" || key === "not_rechecked") return f[key] as FindingBrief[];
  return [...f.fixed, ...f.still_present, ...f.changed, ...f.new].filter((r) => (r.change ?? "still_present") === key);
};

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
  const [rfMode, setRfMode] = useState<"board" | "field" | "pattern">("board");
  const [pview, setPview] = useState<PatternViewKey>("persp");
  const [pickA, setPickA] = useState<string | null>(null);
  const [pickB, setPickB] = useState<string | null>(null);
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
  // solver runs of each version's review: when both have some, the page overlays their tuning curves and offers the field and pattern slider
  const sims = useCompareSims(cmp?.from.review_id, cmp?.to.review_id);
  const jobA = (sims.a && (sims.a.jobs.find((j) => j.job_id === pickA) ?? bestRun(sims.a.jobs))) || null;
  const jobB = (sims.b && (sims.b.jobs.find((j) => j.job_id === pickB) ?? bestRun(sims.b.jobs))) || null;
  const bothSims = !!(jobA && jobB);
  const dataA = useRunData(sims.a, jobA, bothSims && rfMode === "field", bothSims && rfMode === "pattern");
  const dataB = useRunData(sims.b, jobB, bothSims && rfMode === "field", bothSims && rfMode === "pattern");
  const fieldOk = !!(jobA?.files.includes("field.json") && jobB?.files.includes("field.json"));
  const patternOk = !!(jobA?.files.includes("pattern.json") && jobB?.files.includes("pattern.json"));
  const mode = bothSims ? rfMode : "board";
  const rfBand = useMemo(() => { const bd = dataB.summary?.setup?.band_ghz ?? dataA.summary?.setup?.band_ghz ?? dataB.meta?.setup?.band_ghz; return bd ? { lo_ghz: bd[0], hi_ghz: bd[1], target_db: -10 } : null; }, [dataA.summary, dataB.summary, dataB.meta]);
  const antennaOf = (m: typeof dataA.meta, bd: BBox) => { const q = m?.setup?.antenna_bbox; return (q && q.length === 4 ? [(q[0] + q[2]) / 2, 3, (q[1] + q[3]) / 2] : [(bd[0] + bd[2]) / 2, 3, (bd[1] + bd[3]) / 2]) as [number, number, number]; };
  const refDbi = Math.max(dataA.pattern?.dmax_dbi ?? -99, dataB.pattern?.dmax_dbi ?? -99);
  const marksA = useMemo(() => asMarks(cmp?.board ? (cmp.board as unknown as { marks?: { from: Mark[] } }).marks?.from : undefined, "info", "a"), [cmp]);
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
      <main id="main" tabIndex={-1} className="pg-main" style={{ maxWidth: 1440 }}>
        <div className="pg-head">
          <div>
            <span className="idx">{cmp ? `Version ${nA} against version ${nB}` : "Compare versions"}</span>
            <h1 className="heading">What changed between versions.</h1>
          </div>
          <span className="grow" />
          {bothSims && (
            <div className="seg" role="group" aria-label="What the slider compares" style={{ marginRight: 12 }}>
              <button aria-pressed={mode === "board"} onClick={() => setRfMode("board")}><Icon name="board" size={13} /> Board</button>
              <button aria-pressed={mode === "field"} disabled={!fieldOk} title={fieldOk ? undefined : "One of the chosen runs kept no field files"} onClick={() => setRfMode("field")}><Icon name="wave" size={13} /> Field</button>
              <button aria-pressed={mode === "pattern"} disabled={!patternOk} title={patternOk ? undefined : "One of the chosen runs has no radiation pattern"} onClick={() => setRfMode("pattern")}><Icon name="cube" size={13} /> Pattern</button>
            </div>
          )}
          {cmp && <span className="pg-head-actions"><span className="stamp solid">A: version {nA}</span><span className="stamp solid">B: version {nB}</span></span>}
        </div>
        {error && <p className="chat-err" role="alert">{error}</p>}
        {!error && !cmp && <p className="dim">Comparing.</p>}
        {cmp && (
          <div className="cmp">
            <div className="cmp-board" ref={box} onPointerMove={move} onPointerUp={() => { drag.current = false; }} onPointerLeave={() => { drag.current = false; }}>
              {a && b && (
                <>
                  {mode === "pattern" ? (
                    <>
                      <div className="cmp-layer"><PatternLayer board={a.board} pattern={dataA.pattern} center={antennaOf(dataA.meta, a.board.bbox)} view={pview} refDbi={refDbi} /></div>
                      <div className="cmp-layer top" style={{ clipPath: `inset(0 0 0 ${split}%)` }}><PatternLayer board={b.board} pattern={dataB.pattern} center={antennaOf(dataB.meta, b.board.bbox)} view={pview} refDbi={refDbi} /></div>
                    </>
                  ) : (
                    <>
                      <div className="cmp-layer"><Board2D board={a.board} marks={mode === "field" ? [] : marksA} meta={{}} selectedId={null} onSelect={() => {}} layers={DEFAULT_LAYERS} insets={none} interactive={false} chrome={false} externalView={view} onViewChange={setView} fitTo={fit} scene={sceneA} overlay={mode === "field" && dataA.fields?.xy ? () => <FieldCanvas2D slice={dataA.fields!.xy!} /> : undefined} /></div>
                      <div className="cmp-layer top" style={{ clipPath: `inset(0 0 0 ${split}%)` }}><Board2D board={b.board} marks={mode === "field" ? [] : marksB} meta={{}} selectedId={null} onSelect={() => {}} layers={DEFAULT_LAYERS} insets={none} onViewChange={setView} externalView={view} fitTo={fit} scene={sceneB} chrome={false} overlay={mode === "field" && dataB.fields?.xy ? () => <FieldCanvas2D slice={dataB.fields!.xy!} /> : undefined} /></div>
                    </>
                  )}
                  <div className="cmp-handle" style={{ left: `${split}%` }} onPointerDown={(e) => { drag.current = true; (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); }}>
                    <span /><b><Icon name="compare" size={14} /></b>
                  </div>
                  <span className="cmp-lab a stamp solid">A / v{nA}</span>
                  <span className="cmp-lab b stamp solid">B / v{nB}</span>
                  {mode !== "board" && (
                    <div className="cmp-note">
                      {mode === "field" ? (
                        <>
                          <span className="idx">Field, E-z</span>
                          <span>A {dataA.fields?.f_hz ? describeFreq(dataA.fields.f_hz, rfBand ? [rfBand.lo_ghz, rfBand.hi_ghz] : null) : "loading"}{isScreen(jobA) ? " (screen)" : ""}</span>
                          <span>B {dataB.fields?.f_hz ? describeFreq(dataB.fields.f_hz, rfBand ? [rfBand.lo_ghz, rfBand.hi_ghz] : null) : "loading"}{isScreen(jobB) ? " (screen)" : ""}</span>
                          <span className="dim">Each field has its own relative scale: compare where the energy goes, not how strong it is.</span>
                        </>
                      ) : (
                        <>
                          <span className="idx">Radiation pattern, same dB scale</span>
                          <span>A {dataA.pattern ? `${dataA.pattern.dmax_dbi.toFixed(1)} dBi peak at ${(dataA.pattern.f_hz / 1e9).toFixed(3)} GHz` : "loading"}</span>
                          <span>B {dataB.pattern ? `${dataB.pattern.dmax_dbi.toFixed(1)} dBi peak at ${(dataB.pattern.f_hz / 1e9).toFixed(3)} GHz` : "loading"}</span>
                          <span className="dim">Each pattern is taken at its own deepest S11 dip, so the two frequencies can differ. Screen runs: compare shapes only.</span>
                          <span className="seg" role="group" aria-label="Camera" style={{ pointerEvents: "auto" }}>
                            {(Object.keys(PATTERN_VIEWS) as PatternViewKey[]).map((k) => (<button key={k} aria-pressed={pview === k} onClick={() => setPview(k)}>{k === "persp" ? "3/4" : k}</button>))}
                          </span>
                        </>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>

            <div className="cmp-side">
              <Panel className="card" title="Open findings">
                <div className="cmp-stack">
                  <div className="cmp-row"><span className="idx">A, version {nA}</span>{cmp.from.counts ? <SevBar counts={cmp.from.counts} size="sm" /> : <span className="dim">no review</span>}</div>
                  <div className="cmp-row"><span className="idx">B, version {nB}</span>{cmp.to.counts ? <SevBar counts={cmp.to.counts} size="sm" /> : <span className="dim">no review</span>}</div>
                  {!bothSims && <p className="idx" style={{ whiteSpace: "normal", lineHeight: 1.5, margin: 0 }}>{sims.done ? "The deepest dip is not compared. Both versions need a simulation in their reviews for that." : "Looking for simulations."}</p>}
                </div>
              </Panel>
              {cmp.version_change && <VersionChange vc={cmp.version_change} compact />}
              {bothSims && !cmp.version_change && (<CompareHeadline jobA={jobA} jobB={jobB} dataA={dataA} dataB={dataB} nA={nA} nB={nB} band={rfBand} />)}
              {bothSims && sims.a && sims.b && (
                <CompareTuning sourceA={sims.a} sourceB={sims.b} jobA={jobA} jobB={jobB} onPickA={setPickA} onPickB={setPickB} dataA={dataA} dataB={dataB} nA={nA} nB={nB} band={rfBand} />
              )}
              <Panel className="card" title="Findings">
                {cmp.findings ? (
                  <>
                    {cmp.findings.counts && (
                      <p className="idx cmp-counts" aria-label="Findings by change">{(["fixed", "improved", "still_present", "worse", "new"] as const).map((k) => `${cmp.findings!.counts![k]} ${k.replace("_", " ")}`).join(" / ")}</p>
                    )}
                    {GROUPS.map((g) => {
                      const rows = groupOf(cmp.findings!, g.key);
                      if (!rows.length) return null;
                      return (
                        <div key={g.key}>
                          <div className="cmp-group-h">{g.label} ({rows.length})</div>
                          <ul className="cmp-list">
                            {rows.map((r) => (
                              <li key={r.id} className={g.key}>
                                <span className="idx">{r.code}</span>
                                <span className="cmp-t">{r.title}</span>
                                <span className="cmp-s"><SevGlyph sev={(r.was?.severity ?? r.severity) as Severity} size={10} />{r.was && r.was.severity !== r.severity && <><i>&rarr;</i><SevGlyph sev={r.severity} size={10} /></>}</span>
                                {r.changes && r.changes.length > 0 && <span className="dim cmp-v">{r.changes.join("; ")}</span>}
                                {r.note && <span className="dim cmp-v">{r.note}</span>}
                                {r.dismissed_reason && <span className="dim cmp-v">Dismissed: {r.dismissed_reason}</span>}
                              </li>
                            ))}
                          </ul>
                        </div>
                      );
                    })}
                    {GROUPS.every((g) => !groupOf(cmp.findings!, g.key).length) && <p className="empty">Neither version has findings.</p>}
                  </>
                ) : <p className="empty">Both versions need a finished review to compare findings.</p>}
              </Panel>
              <Panel className="card" title="Board differences">
                {diff ? (
                  <ul className="cmp-list">
                    <li><span className="idx">Parts</span><span className="cmp-t">{diff.parts.added.length} added, {diff.parts.removed.length} removed, {diff.parts.moved.length} moved, {diff.parts.value_changed.length} value changed</span></li>
                    {diff.parts.moved.slice(0, 5).map((m) => (<li key={m.ref}><span className="idx">{m.ref}</span><span className="cmp-t">moved {m.distance_mm} mm</span></li>))}
                    {diff.parts.value_changed.slice(0, 5).map((m) => (<li key={m.ref}><span className="idx">{m.ref}</span><span className="cmp-t">{m.from} to {m.to}</span></li>))}
                    <li><span className="idx">Tracks</span><span className="cmp-t">+{diff.tracks.added} / -{diff.tracks.removed} ({diff.tracks.length_added_mm} / {diff.tracks.length_removed_mm} mm)</span></li>
                    <li><span className="idx">Vias</span><span className="cmp-t">{diff.vias.count_old} to {diff.vias.count_new}</span></li>
                    <li><span className="idx">Zones</span><span className="cmp-t">{diff.zones.old} to {diff.zones.new}</span></li>
                    {diff.nets.added.length + diff.nets.removed.length > 0 && <li><span className="idx">Nets</span><span className="cmp-t">+{diff.nets.added.join(", ") || "none"} / -{diff.nets.removed.join(", ") || "none"}</span></li>}
                  </ul>
                ) : <p className="empty">Board differences are not available.</p>}
                {cmp.notes.length > 0 && <p className="dim" style={{ fontSize: 12.5, margin: "8px 0 0" }}>{cmp.notes.join(" ")}</p>}
                <p className="dim" style={{ fontSize: 12.5, margin: "8px 0 0" }}>Grey marks on A are removed copper and parts. Amber marks on B are added or moved ones. Dismissals carry forward by fingerprint.</p>
              </Panel>
              {cmp.to.review_id && <Link className="btn primary" href={`/reviews/${cmp.to.review_id}`}><Icon name="board" size={14} /> Open version {nB} in the review</Link>}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export function Compare() {
  if (CompareFixture) return <CompareFixture />;
  return (<Suspense fallback={null}><CompareLive /></Suspense>);
}
