"use client";
import "./report.css";
import Link from "next/link";
import { Fragment, useEffect, useMemo, useState } from "react";
import { getBoard, getCompare, getEvidence, getReport, getReviewReplay, resolveCurve, TARGET_BAND } from "../../lib/fixture-api";
import { bboxUnion, indexBoard, resolveMark } from "../../lib/geometry.mjs";
import { loadPatchSim, scenarioBoard, type RfBundle } from "../../lib/rf-data";
import { replayAll, severityCounts, sortFindings } from "../../lib/review-reducer.mjs";
import { buildMeta, findingLabel, findingNumber, fmtClock } from "../../lib/review-view";
import type { BBox, Board, Geometry } from "../../lib/types";
import { Board2D, type ActiveMarkItem } from "../board2d/Board2D";
import { DEFAULT_LAYERS } from "../board2d/layers";
import { FieldCanvas2D } from "../rf/FieldCanvas2D";
import { SmithChart, TuningGraph, minOf } from "../rf/charts";
import { ClassStamp, FixtureBadge, Icon, SevBar, SevGlyph } from "../ui";

type Rep = Awaited<ReturnType<typeof getReport>>;
const none = { l: 0, r: 0, t: 0, b: 0 };
const noop = () => {};

function Plate({ board, marks, meta, selectedId, fit, height, overlay, caption }: {
  board: Board; marks: ActiveMarkItem[]; meta: ReturnType<typeof buildMeta>; selectedId?: string | null; fit?: BBox | null; height: number;
  overlay?: () => React.ReactNode; caption?: React.ReactNode;
}) {
  return (
    <figure style={{ margin: 0 }}>
      <div className="plate" data-theme="ink" style={{ height }}>
        <Board2D board={board} marks={marks} meta={meta} selectedId={selectedId ?? null} onSelect={noop} layers={DEFAULT_LAYERS} insets={none} interactive={false} chrome={false} fitTo={fit ?? null} overlay={overlay} />
      </div>
      {caption && <figcaption className="plate-cap">{caption}</figcaption>}
    </figure>
  );
}

export function ReportFixture({ reviewId }: { reviewId: string }) {
  const [rep, setRep] = useState<Rep | null>(null);
  const [board, setBoard] = useState<Board | null>(null);
  const [rf, setRf] = useState<RfBundle | null>(null);
  const [state, setState] = useState<ReturnType<typeof replayAll> | null>(null);
  const [cmp, setCmp] = useState<Awaited<ReturnType<typeof getCompare>> | null>(null);

  useEffect(() => {
    getReport(reviewId).then(setRep);
    getBoard().then(setBoard);
    getCompare("ver-1", "ver-2").then(setCmp);
    getReviewReplay(reviewId).then((s) => setState(replayAll(s.events)));
      }, [reviewId]);
  useEffect(() => { loadPatchSim().then(setRf); }, []);
  const patchBoard = useMemo(() => (rf ? scenarioBoard(rf) : null), [rf]);

  const findings = useMemo(() => (rep ? sortFindings(rep.findings) : []), [rep]);
  const meta = useMemo(() => buildMeta(rep?.findings ?? []), [rep]);
  const index = useMemo(() => (board ? indexBoard(board) : null), [board]);
  const allMarks: ActiveMarkItem[] = useMemo(() => findings.flatMap((f) => f.marks.map((m, i) => ({ key: `${f.id}:${i}`, findingId: f.id, severity: f.severity, mark: m }))), [findings]);
  const fitOf = (ids: string[]): BBox | null => {
    if (!index) return null;
    const g = ids.flatMap((t) => resolveMark(index, { targets: [t] }) as Geometry[]);
    const u = bboxUnion(g.map((x) => x.bbox));
    return u ? ([u[0] - 6, u[1] - 5, u[2] + 6, u[3] + 5] as BBox) : null;
  };

  if (!rep || !board) return <div className="rp"><div className="rp-page" style={{ paddingTop: 60 }}><span className="idx">Assembling the report</span></div></div>;
  const counts = severityCounts(rep.findings);
  const mn = rf ? minOf(rf.s11) : null;
  const inBand = rf && mn ? mn.f >= rf.band.lo_ghz && mn.f <= rf.band.hi_ghz && mn.db <= rf.band.target_db : false;
  const baseCurve = resolveCurve("base")!;
  const confirm = resolveCurve("confirm")!;
  const inv = state?.investigationOrder.map((id) => state.investigations[id]) ?? [];
  const date = new Date(rep.generated_at).toISOString().slice(0, 10);

  return (
    <div className="rp">
      <div className="rp-bar no-print">
        <Link className="btn sm" href={`/reviews/${reviewId}`}><Icon name="back" size={13} /> Back to review</Link>
        <div className="rp-bar-title"><b>Report r{rep.revision}</b></div>
        <span style={{ flex: 1 }} />
        <FixtureBadge label="Fixture report" />
        <button className="btn sm primary" onClick={() => window.print()}><Icon name="print" size={13} /> Print or save as PDF</button>
      </div>
      <div className="rp-sheet" data-theme="paper">
      <div className="rp-page">
        {/* 1 cover */}
        <section className="rp-cover">
          <span className="kicker">TrustPCB inspection report, r{rep.revision}</span>
          <h1 className="serif">{rep.review.project_name}</h1>
          <p className="dim" style={{ margin: 0, maxWidth: "52ch" }}>{rep.review.goal_text}</p>
          <div className="rp-meta">
            <div><span className="idx">Version</span><b>{rep.review.version_label}</b></div>
            <div><span className="idx">Date</span><b>{date}</b></div>
            <div><span className="idx">Profile</span><b>{rep.review.depth}</b></div>
            <div><span className="idx">End state</span><b>Complete</b></div>
            <div><span className="idx">Review</span><b>{rep.review.id}</b></div>
          </div>
          <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}><SevBar counts={counts} /><span className="stamp fixture">Dev fixture. Not a real review.</span></div>
        </section>

        {/* 2 summary */}
        <section className="rp-sec">
          <header><h2 className="serif">Summary</h2></header>
          <p>{rep.summary}</p>
          <span className="idx">Top actions</span>
          <ol className="rp-actions" style={{ marginTop: 12, paddingBottom: 30 }}>{rep.actions.map((a) => (<li key={a}>{a}</li>))}</ol>
        </section>

        {/* 3 overview */}
        <section className="rp-sec">
          <header><h2 className="serif">Board overview</h2></header>
          <Plate board={board} marks={allMarks} meta={meta} height={430} caption={<><span>66.0 x 53.0 mm. 2 copper layers. Numbered balloons match the findings below.</span><span>Top view</span></>} />
          <div className="rp-grid2" style={{ marginTop: 24 }}>
            <div>
              <span className="idx">Open findings by severity</span>
              <table className="rp-tbl" style={{ marginTop: 8 }}><tbody>
                {(["critical", "high", "medium", "low", "info"] as const).map((s) => (<tr key={s}><td style={{ width: 24 }}><SevGlyph sev={s} /></td><td style={{ textTransform: "capitalize" }}>{s}</td><td className="num" style={{ textAlign: "right" }}>{counts[s]}</td></tr>))}
              </tbody></table>
              <p className="dim" style={{ fontSize: 12 }}>There is no overall board score. It would look more precise than it is.</p>
            </div>
            <div>
              <span className="idx">Zones</span>
              <ul className="rp-list" style={{ marginTop: 8 }}>{Object.entries(state?.brief?.zones ?? {}).map(([k, v]) => (<li key={k}><b>{k}</b> <span className="dim">{(v as string[]).join(", ")}</span></li>))}</ul>
              <p style={{ fontSize: 13 }}>{state?.brief?.purpose}</p>
            </div>
          </div>
        </section>

        {/* 4 RF */}
        <section className="rp-sec">
          <header><h2 className="serif">RF result</h2><ClassStamp cls="simulated" /></header>
          <div className="rp-grid2">
            <div>
              <span className="idx">This review: S11, base and confirmed fix</span> <span className="stamp fixture">Fixture curves</span>
              <TuningGraph curves={[{ id: "base", label: "Base", ...baseCurve }, { id: "c", label: "Shorten 4 mm", ...confirm }]} band={TARGET_BAND} height={230} />
              <p className="dim" style={{ fontSize: 11 }}>Target {TARGET_BAND.lo_ghz} to {TARGET_BAND.hi_ghz} GHz, better than {TARGET_BAND.target_db} dB. Illustrative until this review's solver result is wired in.</p>
            </div>
            <div>
              <span className="idx">Real solver sample: patch antenna S11</span> <span className="stamp sim"><i />openEMS</span>
              {rf && <TuningGraph curves={[{ id: "p", label: "Patch", ...rf.s11 }]} band={rf.band} height={230} />}
              {rf && mn && <p className="dim" style={{ fontSize: 11 }}>Deepest dip {mn.f.toFixed(3)} GHz, S11 {mn.db.toFixed(1)} dB, {inBand ? "inside" : "outside"} the +/- 50 MHz display band around the scenario frequency {(rf.f0_hz / 1e9).toFixed(2)} GHz.</p>}
            </div>
          </div>
          {rf && patchBoard && (
            <div className="rp-grid2">
              <div>
                <span className="idx">Smith chart, 50 ohm</span>
                <SmithChart zin={rf.zin} band={rf.band} size={300} />
              </div>
              <Plate board={patchBoard} marks={[]} meta={{}} height={300} fit={[-40, -34, 40, 34]} overlay={() => rf.fieldXY ? <FieldCanvas2D slice={rf.fieldXY} paused opacity={0.95} /> : null} caption={<><span>Real field, one phase still, plane z = {rf.fieldXY?.at_mm.toFixed(1)} mm.</span><span>{(rf.f0_hz / 1e9).toFixed(2)} GHz</span></>} />
            </div>
          )}
          <p className="idx" style={{ margin: "14px 0 26px" }}>Simulated. Not a substitute for a real antenna measurement.</p>
        </section>

        {/* 5 investigation */}
        <section className="rp-sec">
          <header><h2 className="serif">Investigation story</h2></header>
          {inv.map((v) => (
            <div key={v.id} style={{ marginBottom: 24 }}>
              <p className="serif" style={{ fontSize: 24, lineHeight: 1.2, margin: "0 0 12px" }}>{v.question}</p>
              {v.suspects.map((s, i) => (<div key={s.id} className="rp-sus"><span className="idx">S{i + 1}</span><span>{s.title}{s.conclusion ? <span className="dim"> . {s.conclusion}</span> : ""}</span><span className="stamp">{s.status.replace("_", " ")}</span></div>))}
            </div>
          ))}
          <table className="rp-tbl" style={{ marginBottom: 30 }}>
            <thead><tr><th>Run</th><th>Change</th><th>f0</th><th>S11</th><th>Trust</th></tr></thead>
            <tbody>{state?.experiments.map((e) => (
              <tr key={e.id}><td>{e.label}</td><td className="dim">{e.quality ?? "none"}</td><td className="num">{((e.measurements.f_res_hz?.value ?? 0) / 1e9).toFixed(3)} GHz</td><td className="num">{(e.measurements.s11_min_db?.value ?? 0).toFixed(1)} dB</td><td>{e.trust}</td></tr>
            ))}</tbody>
          </table>
        </section>

        {/* 6 findings */}
        <section className="rp-sec">
          <header><h2 className="serif">Findings</h2></header>
          {findings.map((f) => {
            const ev = getEvidence(f.evidence_ids ?? []);
            return (
              <article key={f.id} className="rp-find">
                <Plate board={board} marks={allMarks} meta={meta} selectedId={f.id} height={260} fit={fitOf(f.marks.flatMap((m) => m.targets))} />
                <div>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span className={`rp-balloon ${f.severity}`} style={{ marginRight: 0 }}>{findingNumber(f.id)}</span><span className="idx">{findingLabel(f.id)}</span><SevGlyph sev={f.severity} /><span className="idx">{f.severity}</span><ClassStamp cls={f.class} />
                  </div>
                  <h3>{f.title}</h3>
                  <span className="idx sub" style={{ display: "block" }}>What</span><p>{f.what}</p>
                  <span className="idx sub" style={{ display: "block" }}>Why it matters</span><p>{f.why_it_matters}</p>
                  <span className="idx sub" style={{ display: "block" }}>Recommendation</span><p>{f.recommendation}</p>
                  {ev.length > 0 && (<><span className="idx sub" style={{ display: "block" }}>Evidence</span><ul>{ev.map((e) => (<li key={e.id}><span className="code">{e.id}</span> {e.source_tool}: {Object.entries(e.values).map(([k, v]) => `${k.replace(/_/g, " ")} ${v.value} ${v.unit}`).join(", ")}</li>))}</ul></>)}
                </div>
              </article>
            );
          })}
        </section>

        {/* 7 version changes */}
        {cmp && (
          <section className="rp-sec">
            <header><h2 className="serif">Version changes</h2></header>
            <table className="rp-tbl" style={{ marginBottom: 30 }}>
              <thead><tr><th>Change</th><th>Finding</th><th>{cmp.a.label}</th><th>{cmp.b.label}</th></tr></thead>
              <tbody>{cmp.rows.map((r) => (<tr key={r.id}><td><span className="stamp">{r.change.replace("_", " ")}</span></td><td><span className="idx">{r.id}</span> {r.title}</td><td className="num">{r.a}</td><td className="num">{r.b}</td></tr>))}</tbody>
            </table>
          </section>
        )}

        {/* 8 limits */}
        <section className="rp-sec">
          <header><h2 className="serif">Not tested, assumptions, limits</h2></header>
          <div className="rp-two" style={{ paddingBottom: 30 }}>
            <div><span className="idx">Not tested</span><ul className="rp-list" style={{ marginTop: 8 }}>{rep.not_tested.map((n) => (<li key={n}>{n}</li>))}</ul></div>
            <div><span className="idx">Assumptions</span><ul className="rp-list" style={{ marginTop: 8 }}>{rep.assumptions.map((a) => (<li key={a.id}>{a.text} <span className="stamp">{a.source}</span></li>))}</ul></div>
          </div>
        </section>

        {/* 9 method */}
        <section className="rp-sec">
          <header><h2 className="serif">Method</h2></header>
          <dl className="kv" style={{ gridTemplateColumns: "140px 1fr", paddingBottom: 20 }}>{rep.method.map(([k, v]) => (<Fragment key={k}><dt>{k}</dt><dd style={{ fontSize: 12.5 }}>{v}</dd></Fragment>))}</dl>
          <p className="dim" style={{ fontSize: 12 }}>Numbers in this report are assembled by code from stored evidence, so they match the app. Run time shown: {state?.budget?.wall_s ? fmtClock(state.budget.wall_s.used) : "-"}.</p>
        </section>
        <footer className="rp-foot"><span>TrustPCB inspects. It never edits a design.</span><span>r{rep.revision}, {date}</span></footer>
      </div>
      </div>
    </div>
  );
}
