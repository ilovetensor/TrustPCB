"use client";
import "./pages.css";
import "./compare.css";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { getBoard, getCompare, getFinding, TARGET_BAND } from "../../lib/fixture-api";
import { buildMeta } from "../../lib/review-view";
import type { Board, Finding, Mark, Severity } from "../../lib/types";
import { Board2D, type ActiveMarkItem } from "../board2d/Board2D";
import { DEFAULT_LAYERS } from "../board2d/layers";
import { TuningGraph, minOf } from "../rf/charts";
import { Icon, Panel, SevGlyph, TopBar } from "../ui";

type Cmp = Awaited<ReturnType<typeof getCompare>>;
const none = { l: 0, r: 0, t: 0, b: 0 };
const CH: Record<string, string> = { fixed: "Fixed", new: "New", still_present: "Still present", changed: "Changed" };

function toMarks(fs: Finding[]): ActiveMarkItem[] {
  return fs.flatMap((f) => f.marks.map((m, i) => ({ key: `${f.id}:${i}`, findingId: f.id, severity: f.severity, mark: m })));
}

export function CompareFixture() {
  const [board, setBoard] = useState<Board | null>(null);
  const [cmp, setCmp] = useState<Cmp | null>(null);
  const [split, setSplit] = useState(50);
  const [view, setView] = useState<{ scale: number; tx: number; ty: number } | null>(null);
  const [sel, setSel] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const drag = useRef(false);

  useEffect(() => { getBoard().then(setBoard); getCompare("ver-1", "ver-2").then(setCmp); }, []);

  const { a, b } = useMemo(() => {
    const f = (id: string) => getFinding(id)!;
    const f002a: Finding = { ...f("F-002"), severity: "high", marks: f("F-002").marks.map((m) => (m.value ? { ...m, value: { value: 0.1, unit: "mm" } } : m)), title: "Antenna feed ends 0.10 mm from the pour boundary" };
    const f009: Finding = {
      ...f("F-006"), id: "F-009", title: "Ground pour on the back layer was missing", severity: "medium", class: "measured",
      marks: [{ type: "pin", targets: ["zone:2efe2b2e"], label: "No B.Cu pour in v1" } as Mark],
    };
    const A = [f("F-001"), f002a, f("F-007"), f009];
    const B = [f("F-001"), f("F-002"), f("F-007"), f("F-008")];
    return { a: A, b: B };
  }, []);
  const metaA = useMemo(() => buildMeta(a), [a]);
  const metaB = useMemo(() => buildMeta(b), [b]);
  const marksA = useMemo(() => toMarks(a), [a]);
  const marksB = useMemo(() => toMarks(b), [b]);

  const move = (e: React.PointerEvent) => {
    if (!drag.current || !box.current) return;
    const r = box.current.getBoundingClientRect();
    setSplit(Math.min(96, Math.max(4, ((e.clientX - r.left) / r.width) * 100)));
  };
  const curves = cmp ? [{ id: "a", label: `A ${cmp.a.label}`, ...cmp.a.curve }, { id: "b", label: `B ${cmp.b.label}`, ...cmp.b.curve }] : [];
  const d = cmp ? (cmp.b.f0_ghz - cmp.a.f0_ghz) * 1000 : 0;

  return (
    <div className="pg">
      <TopBar current="compare" />
      <main id="main" tabIndex={-1} className="pg-main" style={{ maxWidth: 1440 }}>
        <div className="pg-head">
          <div>
            <span className="idx">nRF52840 RF IoT node</span>
            <h1 className="heading">What changed between versions.</h1>
          </div>
          <span className="grow" />
          <span className="pg-head-actions"><span className="stamp solid">A: version 1</span><span className="stamp solid">B: version 2</span></span>
        </div>

        <div className="cmp">
          <div className="cmp-board" ref={box} onPointerMove={move} onPointerUp={() => { drag.current = false; }} onPointerLeave={() => { drag.current = false; }}>
            {board && (
              <>
                <div className="cmp-layer"><Board2D board={board} marks={marksA} meta={metaA} selectedId={sel} onSelect={setSel} layers={DEFAULT_LAYERS} insets={none} interactive={false} chrome={false} externalView={view} onViewChange={setView} /></div>
                <div className="cmp-layer top" style={{ clipPath: `inset(0 0 0 ${split}%)` }}><Board2D board={board} marks={marksB} meta={metaB} selectedId={sel} onSelect={setSel} layers={DEFAULT_LAYERS} insets={none} onViewChange={setView} externalView={view} /></div>
                <div className="cmp-handle" style={{ left: `${split}%` }} onPointerDown={(e) => { drag.current = true; (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId); }}>
                  <span /><b><Icon name="compare" size={14} /></b>
                </div>
                <span className="cmp-lab a stamp solid">A / v1</span>
                <span className="cmp-lab b stamp solid">B / v2</span>
              </>
            )}
          </div>

          <div className="cmp-side">
            <Panel className="card" title="Tuning, A over B">
              <div>
                {cmp && <TuningGraph curves={curves} band={TARGET_BAND} height={190} />}
                {cmp && (
                  <p style={{ fontSize: 12.5, margin: "10px 0 0" }}>
                    {cmp.a.f0_ghz.toFixed(3)} GHz &rarr; {cmp.b.f0_ghz.toFixed(3)} GHz <b className={d > 0 ? "" : "signal"}>{d > 0 ? "+" : ""}{d.toFixed(0)} MHz</b>
                    <span className="dim"> &nbsp; min {minOf(cmp.a.curve).db.toFixed(1)} to {minOf(cmp.b.curve).db.toFixed(1)} dB</span>
                  </p>
                )}
                <p className="idx" style={{ marginTop: 8 }}>Fixture curves</p>
              </div>
            </Panel>
            <Panel className="card" title="Findings">
              <ul className="cmp-list">
                {cmp?.rows.map((r) => (
                  <li key={r.id} className={r.change}>
                    <span className="stamp">{CH[r.change]}</span>
                    <span className="idx">{r.id}</span>
                    <span className="cmp-t">{r.title}</span>
                    <span className="cmp-v">{r.a} <i>&rarr;</i> {r.b}</span>
                    <span className="cmp-s"><SevGlyph sev={r.sevA as Severity} size={10} /><i>&rarr;</i><SevGlyph sev={r.sevB as Severity} size={10} /></span>
                  </li>
                ))}
              </ul>
              <p className="dim" style={{ fontSize: 12.5, margin: "8px 0 0" }}>Dismissals carry forward by fingerprint. Board copper and part differences are highlighted when the geometry differs. Both versions show the same fixture board here.</p>
            </Panel>
            <Link className="btn primary" href="/reviews/r-0042"><Icon name="board" size={14} /> Open version 2 in the review</Link>
          </div>
        </div>
      </main>
    </div>
  );
}
