// DEV-ONLY FIXTURE. Review data for the bundled nRF52840 node board.
// Geometry-derived numbers (distances, lengths) were measured from the board file.
// Simulation numbers and curves are ILLUSTRATIVE placeholders. Live pages read the real solver output through
// lib/api.ts and lib/sims.ts instead. The UI labels this everywhere.

import type { Evidence, Finding } from "../types";

export const FIXTURE_NOTE = "Dev fixture. Not a real review.";

export const NRF_ASSETS = {
  board: "/fixtures/nrf/board.json",
  glb: "/fixtures/nrf/board.glb.gz",
};

/* ----------------------------- curves (illustrative) ----------------------------- */

export interface Curve { f_ghz: number[]; s11_db: number[] }

function lorentz(f0: number, depth: number, width: number, base = -1.2): Curve {
  const f_ghz: number[] = [];
  const s11_db: number[] = [];
  for (let i = 0; i <= 80; i++) {
    const f = 2.0 + (i / 80) * 0.8;
    const x = (f - f0) / width;
    const dip = depth / (1 + x * x);
    // a gentle second ripple so it reads like a measured curve, not a parabola
    const ripple = 0.35 * Math.sin(f * 21) * Math.exp(-Math.abs(f - f0) * 3);
    f_ghz.push(+f.toFixed(4));
    s11_db.push(+(base - dip + ripple).toFixed(2));
  }
  return { f_ghz, s11_db };
}

export const TARGET_BAND = { lo_ghz: 2.4, hi_ghz: 2.4835, target_db: -10 };

export const CURVES = {
  base: lorentz(2.312, 13.4, 0.055),
  clear: lorentz(2.351, 9.2, 0.07),
  shorten: lorentz(2.436, 17.1, 0.05),
  confirm: lorentz(2.431, 16.2, 0.048),
  v1: lorentz(2.285, 9.8, 0.06),
};

/* ----------------------------- findings ----------------------------- */

export const FINDINGS: Record<string, Finding> = {
  "F-001": {
    id: "F-001", fingerprint: "antenna_detuned|net:ANT_FEED|zone:rf",
    title: "Antenna resonates 120 MHz below the band",
    severity: "high", class: "simulated", status: "open",
    what: "The meander antenna on ANT_FEED has its S11 minimum at 2.31 GHz. The target band starts at 2.40 GHz, so the lower half of the band sits above -10 dB.",
    why_it_matters: "A detuned antenna wastes transmit power and shortens range. The board would pass a bench power check yet lose link margin at the band edge.",
    recommendation: "Shorten the radiating meander by about 4 mm, then re-tune with the matching network. The confirm run of this change put the minimum at 2.43 GHz.",
    evidence_ids: ["E-106", "E-107"],
    marks: [
      { type: "path", targets: ["net:ANT_FEED"], label: "Antenna feed and meander" },
      { type: "pin", targets: ["track:2473fb93"], label: "Resonance 2.31 GHz", value: { value: 2.31, unit: "GHz" } },
    ],
    assumptions: ["FR-4 permittivity 4.4 (default, not from the board file)"],
    not_tested: ["Case or enclosure detuning", "Battery and hand proximity"],
  },
  "F-002": {
    id: "F-002", fingerprint: "keepout_gap|net:ANT_FEED|keepout:1444dc6c",
    title: "Antenna feed ends 0.25 mm from the pour boundary",
    severity: "medium", class: "measured", status: "open",
    what: "The 0.3 mm feed line leaves the match at L1 and reaches 0.25 mm from the ground pour edge that closes the antenna keep-clear.",
    why_it_matters: "Copper this close to the radiating feed loads it with stray capacitance and pulls the resonance down.",
    recommendation: "Pull the pour boundary back so the clearance is at least 1.0 mm around the feed line.",
    evidence_ids: ["E-105"],
    marks: [
      { type: "region", targets: ["keepout:1444dc6c"], label: "Antenna keep-clear" },
      { type: "link", targets: ["track:05bc5591", "keepout:1444dc6c"], label: "Feed to pour", value: { value: 0.25, unit: "mm" } },
    ],
    assumptions: [], not_tested: [],
  },
  "F-003": {
    id: "F-003", fingerprint: "cap_distance|ref:C12|ref:U3",
    title: "Bulk capacitor C12 is 7.7 mm from the chip supply pad",
    severity: "low", class: "measured", status: "open",
    what: "C12 (4.7 uF) sits 7.66 mm from the nearest 3V3 supply pad of U3 on a shared 0.5 mm track.",
    why_it_matters: "Distance adds series inductance between the capacitor and the pin it should be serving.",
    recommendation: "Place C12 within about 3 mm of the supply pad, or accept it as bulk storage and rely on the 100 nF capacitors next to U3.",
    evidence_ids: ["E-102"],
    marks: [
      { type: "link", targets: ["pad:C12:1", "pad:U3:W1"], label: "C12 to U3 supply", value: { value: 7.66, unit: "mm" } },
    ],
    assumptions: [], not_tested: ["Supply impedance across frequency"],
  },
  "F-004": {
    id: "F-004", fingerprint: "track_detour|net:XTAL_A|ref:Y1",
    title: "Crystal track runs 3.4 times its straight distance",
    severity: "low", class: "measured", status: "open",
    what: "Net XTAL_A is 35.9 mm of copper between Y1 and U3, against a straight distance of 10.4 mm.",
    why_it_matters: "A long crystal track adds stray capacitance and picks up noise, which shifts the oscillator and weakens the 32 MHz clock.",
    recommendation: "Route XTAL_A directly between Y1 and U3 and keep it clear of other signals.",
    evidence_ids: ["E-104"],
    marks: [{ type: "path", targets: ["net:XTAL_A"], label: "XTAL_A, 35.9 mm" }],
    assumptions: [], not_tested: [],
  },
  "F-005": {
    id: "F-005", fingerprint: "match_dnp|ref:C19|ref:C20",
    title: "Matching network has two unpopulated shunt positions",
    severity: "info", class: "opinion", status: "open",
    what: "C19 and C20 are marked DNP. With them empty the match is a single series inductor, so the only tuning knob left is L1.",
    why_it_matters: "A pi network gives room to correct a detuned antenna after the first prototype.",
    recommendation: "Keep the footprints. After measuring a prototype with a network analyser, populate them as needed.",
    evidence_ids: [],
    marks: [{ type: "object", targets: ["ref:C19", "ref:C20", "ref:L1"], label: "Match: L1, C19, C20" }],
    assumptions: [], not_tested: ["Real S11 of a built board"],
  },
  "F-006": {
    id: "F-006", fingerprint: "usb_pair_not_tested|net:USB_D+_MCU",
    title: "USB data pair skew was not tested",
    severity: "info", class: "not_tested", status: "open",
    what: "USB_D+ and USB_D- run on the front layer. Signal-integrity simulation is not part of this version.",
    why_it_matters: "Full-speed USB is forgiving, but a long or unbalanced pair is worth a quick look before a prototype.",
    recommendation: "Check the pair length difference in your layout tool.",
    evidence_ids: [],
    marks: [{ type: "path", targets: ["net:USB_D+_MCU"], label: "USB D+" }],
    assumptions: [], not_tested: ["Differential skew", "Impedance of the pair"],
  },
  "F-007": {
    id: "F-007", fingerprint: "track_detour|net:3V3|zone:power",
    title: "Power track takes a long detour",
    severity: "medium", class: "measured", status: "open",
    what: "One 3V3 branch leaves U3 and reaches the bus 30.7 mm along, though the straight distance is 21.2 mm. It also passes under the RF section on the back layer.",
    why_it_matters: "A longer power path adds resistance and inductance, and a switching supply branch under the antenna feed can couple noise into the RF section.",
    recommendation: "Feed this branch from the 3V3 bus near U2 and keep it out from under the RF section.",
    evidence_ids: ["E-103"],
    marks: [
      { type: "path", targets: ["track:17bf021d", "track:d2d584ee", "track:51c50cfc", "track:fc7ca589", "track:7b681496"], label: "3V3 detour, 30.7 mm" },
    ],
    assumptions: [], not_tested: [],
  },
  "F-008": {
    id: "F-008", fingerprint: "no_stitching|net:ANT_FEED|zone:rf",
    title: "No ground stitching vias near the antenna feed",
    severity: "low", class: "measured", status: "open",
    what: "No ground via lies within 3 mm of the ANT_FEED line between L1 and the antenna.",
    why_it_matters: "Stitching vias give the feed a defined return path and keep the ground planes at the same potential.",
    recommendation: "Add a row of ground vias along both sides of the feed, outside the keep-clear.",
    evidence_ids: ["E-108"],
    marks: [{ type: "pin", targets: ["track:34d53f07"], label: "Feed line" }],
    assumptions: [], not_tested: [],
  },
};

export const EVIDENCE: Record<string, Evidence> = {
  "E-102": { id: "E-102", type: "measurement", source_tool: "measure", summary: "Distance, pad C12:1 to pad U3:W1", values: { distance: { value: 7.66, unit: "mm" } }, trust: "ok" },
  "E-103": { id: "E-103", type: "analyzer", source_tool: "run_analyzer", summary: "Track detour analyzer on net 3V3 branch", values: { copper_length: { value: 30.7, unit: "mm" }, straight_distance: { value: 21.2, unit: "mm" }, ratio: { value: 1.45, unit: "x" } }, trust: "ok" },
  "E-104": { id: "E-104", type: "analyzer", source_tool: "run_analyzer", summary: "Track detour analyzer on net XTAL_A", values: { copper_length: { value: 35.9, unit: "mm" }, straight_distance: { value: 10.4, unit: "mm" } }, trust: "ok" },
  "E-105": { id: "E-105", type: "measurement", source_tool: "measure", summary: "Clearance, feed track to pour boundary", values: { clearance: { value: 0.25, unit: "mm" } }, trust: "ok" },
  "E-106": { id: "E-106", type: "simulation", source_tool: "run_simulation", summary: "Base antenna run, screen quality (illustrative fixture value)", values: { f0: { value: 2.312, unit: "GHz" }, s11_min: { value: -14.6, unit: "dB" } }, trust: "ok" },
  "E-107": { id: "E-107", type: "simulation", source_tool: "confirm_result", summary: "Confirm run, antenna shortened 4 mm (illustrative fixture value)", values: { f0: { value: 2.431, unit: "GHz" }, s11_min: { value: -17.4, unit: "dB" } }, trust: "ok" },
  "E-108": { id: "E-108", type: "analyzer", source_tool: "run_analyzer", summary: "Ground vias within 3 mm of the feed line", values: { via_count: { value: 0, unit: "vias" } }, trust: "ok" },
};

export const ASSUMPTIONS = [
  { id: "A-01", text: "Substrate is FR-4, relative permittivity 4.4, loss tangent 0.02", source: "default" },
  { id: "A-02", text: "Board thickness 1.6 mm", source: "board file" },
  { id: "A-03", text: "Antenna is the meander on net ANT_FEED, fed through L1", source: "detected" },
];

export const NOT_TESTED = [
  "Enclosure and battery detuning",
  "USB differential pair skew and impedance",
  "Thermal behaviour",
  "Measured S11 of a built board",
];

/* ----------------------------- replay timeline ----------------------------- */

export interface TimedEvent { t: number; type: string; payload: Record<string, any>; turn?: string }

const phase = (t: number, p: number, label: string): TimedEvent => ({ t, type: "phase_changed", payload: { phase: p, label } });
const thought = (t: number, text: string): TimedEvent => ({ t, type: "thought", payload: { text } });
const tool = (t: number, id: string, name: string, summary: string, done?: { dt: number; result?: string }): TimedEvent[] => {
  const a: TimedEvent[] = [{ t, type: "tool_start", payload: { call_id: id, tool: name, summary } }];
  if (done) a.push({ t: t + done.dt, type: "tool_result", payload: { call_id: id, tool: name, summary, ok: true, result: done.result } });
  return a;
};
const finding = (t: number, id: string): TimedEvent => ({ t, type: "finding_upserted", payload: { finding: FINDINGS[id] } });
const budget = (t: number, o: Record<string, number>): TimedEvent => ({
  t, type: "budget",
  payload: { wall_total_s: 1800, solver_total_s: 1200, sims_total: 7, turns_total: 90, images_total: 14, ...o },
});

export function buildReplay(): TimedEvent[] {
  const ev: TimedEvent[] = [];
  ev.push({ t: 0, type: "status", payload: { status: "running" } });
  ev.push(budget(0, { wall_left_s: 1800, solver_left_s: 1200, sims_left: 7, turns_used: 0, images_left: 14 }));
  ev.push(phase(0.3, 0, "Intake: unpack and detect"));
  ev.push(...tool(0.9, "c1", "intake", "Unpacked 31 files, found nrf52840_rf_iot_node.kicad_pcb", { dt: 0.8, result: "66.0 x 53.0 mm, 2 copper layers, 50 parts" }));
  ev.push(phase(2.6, 1, "Baseline: DRC and analyzers"));
  ev.push(...tool(3.0, "c2", "kicad_drc", "Ran KiCad design rule check", { dt: 1.2, result: "no violations with the board's own rules" }));
  ev.push({ t: 4.6, type: "candidate_batch", payload: { total: 31, reviewed: 0, by_rule: { track_detour: 6, keepout_gap: 3, cap_distance: 9, no_stitching: 4, other: 9 } } });
  ev.push(...tool(4.8, "c3", "run_analyzer", "Ran 9 analyzers", { dt: 1.4, result: "31 candidates" }));
  ev.push(budget(6, { wall_left_s: 1762, solver_left_s: 1200, sims_left: 7, turns_used: 2, images_left: 14 }));
  ev.push(phase(6.6, 2, "Understand the board"));
  ev.push(thought(7.2, "A 2-layer nRF52840 node with a USB-C port, an LDO, and a printed meander antenna at the top edge. The antenna and its match decide whether this board works."));
  ev.push(...tool(8.0, "c4", "view_board", "Looked at the top copper", { dt: 1.0 }));
  ev.push({
    t: 9.6, type: "brief_ready",
    payload: {
      purpose: "Bluetooth Low Energy sensor node on 2.4 GHz with USB-C power and a printed antenna.",
      zones: [
        { id: "z-rf", label: "RF and antenna", kind: "rf", target: "keepout:1444dc6c" },
        { id: "z-mcu", label: "MCU and clocks", kind: "digital", target: "ref:U3" },
        { id: "z-pwr", label: "USB and power", kind: "power", target: "ref:U2" },
      ],
      priorities: ["Antenna tuning", "RF keep-clear", "Power routing under RF"],
    },
  });
  ev.push(budget(10, { wall_left_s: 1730, solver_left_s: 1200, sims_left: 7, turns_used: 9, images_left: 13 }));
  ev.push(phase(10.6, 3, "Review candidates and look for more"));
  ev.push(thought(11.2, "Grouping the 31 candidates. Six detour candidates look like two real tracks; the rest are short stubs."));
  ev.push(...tool(12.0, "c5", "list_candidates", "Listed candidates by rule", { dt: 0.8 }));
  ev.push(...tool(13.2, "c6", "record_finding", "Recorded F-007: power track detour", { dt: 0.4 }));
  ev.push(finding(13.6, "F-007"));
  ev.push({ t: 13.7, type: "spotlight", payload: { targets: ["track:17bf021d", "track:d2d584ee", "track:51c50cfc", "track:fc7ca589", "track:7b681496"], finding_id: "F-007", reason: "3V3 branch detours under the RF section" } });
  ev.push(thought(15.4, "The detour crosses under the antenna feed on the back layer. That raises its priority."));
  ev.push(...tool(16.2, "c7", "record_finding", "Recorded F-002: feed to pour clearance", { dt: 0.4 }));
  ev.push(finding(16.6, "F-002"));
  ev.push({ t: 16.7, type: "spotlight", payload: { targets: ["track:05bc5591", "keepout:1444dc6c"], finding_id: "F-002", reason: "Feed line meets the pour boundary" } });
  ev.push(...tool(18.4, "c8", "record_finding", "Recorded F-004: crystal track length", { dt: 0.4 }));
  ev.push(finding(18.8, "F-004"));
  ev.push(...tool(19.8, "c9", "record_finding", "Recorded F-003: bulk capacitor distance", { dt: 0.4 }));
  ev.push(finding(20.2, "F-003"));
  ev.push({ t: 20.6, type: "candidate_batch", payload: { total: 31, reviewed: 24, by_rule: {} } });
  ev.push(thought(21.4, "Open review: no ground vias near the feed line. Checking that with the via analyzer."));
  ev.push(...tool(22.2, "c10", "run_analyzer", "Counted ground vias near ANT_FEED", { dt: 0.7, result: "0 vias within 3 mm" }));
  ev.push(finding(23.2, "F-008"));
  ev.push(finding(23.9, "F-005"));
  ev.push(finding(24.6, "F-006"));
  ev.push({ t: 25, type: "candidate_batch", payload: { total: 31, reviewed: 31, by_rule: {} } });
  ev.push(budget(25.4, { wall_left_s: 1650, solver_left_s: 1200, sims_left: 7, turns_used: 31, images_left: 11 }));

  ev.push(phase(26, 4, "Investigate by simulation"));
  ev.push({
    t: 26.8, type: "investigation_updated",
    payload: {
      id: "inv-1", question: "Why does the antenna resonate below the band, and what moves it?", status: "running",
      suspects: [
        { id: "s1", title: "Ground pour near the feed loads the antenna", status: "pending" },
        { id: "s2", title: "Meander is electrically too long", status: "pending" },
        { id: "s3", title: "DNP match pads add stray capacitance", status: "pending" },
      ],
    },
  });
  ev.push({ t: 28, type: "sim_queued", payload: { job_id: "sim-base", label: "Base antenna", kind: "base", quality: "screen" } });
  for (let i = 1; i <= 8; i++) ev.push({ t: 28 + i * 0.7, type: "sim_progress", payload: { job_id: "sim-base", pct: i / 8, step: i < 7 ? "solving" : "far field" } });
  ev.push({ t: 33.9, type: "sim_done", payload: { job_id: "sim-base", summary: { f0_ghz: 2.312, s11_db: -14.6, bw_mhz: 108 }, curve: "base", trust: "ok" } });
  ev.push({ t: 34.2, type: "spotlight", payload: { targets: ["net:ANT_FEED"], finding_id: "F-001", reason: "Base run: resonance 2.31 GHz" } });
  ev.push(thought(34.8, "Base resonance is 2.31 GHz, 120 MHz under the lower band edge. Testing the pour first because it is cheap to change."));
  ev.push({ t: 36, type: "suspect_updated", payload: { investigation_id: "inv-1", suspect_id: "s1", status: "testing" } });
  ev.push({ t: 36.2, type: "sim_queued", payload: { job_id: "sim-clear", label: "Clear pour near feed", kind: "whatif", quality: "screen", change: "clear_copper_region 3 x 12 mm" } });
  for (let i = 1; i <= 6; i++) ev.push({ t: 36.2 + i * 0.6, type: "sim_progress", payload: { job_id: "sim-clear", pct: i / 6, step: "solving" } });
  ev.push({ t: 40.2, type: "sim_done", payload: { job_id: "sim-clear", summary: { f0_ghz: 2.351, s11_db: -10.2, bw_mhz: 120 }, curve: "clear", trust: "ok" } });
  ev.push({ t: 41, type: "suspect_updated", payload: { investigation_id: "inv-1", suspect_id: "s1", status: "ruled_out", conclusion: "Moves resonance 39 MHz, not enough alone" } });
  ev.push({ t: 41.6, type: "suspect_updated", payload: { investigation_id: "inv-1", suspect_id: "s2", status: "testing" } });
  ev.push({ t: 41.8, type: "sim_queued", payload: { job_id: "sim-short", label: "Shorten meander 4 mm", kind: "whatif", quality: "screen", change: "adjust_antenna_length -4 mm" } });
  for (let i = 1; i <= 6; i++) ev.push({ t: 41.8 + i * 0.6, type: "sim_progress", payload: { job_id: "sim-short", pct: i / 6, step: "solving" } });
  ev.push({ t: 45.8, type: "sim_done", payload: { job_id: "sim-short", summary: { f0_ghz: 2.436, s11_db: -18.1, bw_mhz: 96 }, curve: "shorten", trust: "ok" } });
  ev.push({ t: 46.6, type: "suspect_updated", payload: { investigation_id: "inv-1", suspect_id: "s2", status: "confirmed", conclusion: "Resonance moves into the band" } });
  ev.push(budget(47, { wall_left_s: 1520, solver_left_s: 842, sims_left: 4, turns_used: 52, images_left: 9 }));

  ev.push(phase(47.8, 5, "Confirm the best result"));
  ev.push({ t: 48.4, type: "sim_queued", payload: { job_id: "sim-confirm", label: "Confirm: shorten 4 mm", kind: "confirm", quality: "confirm", change: "adjust_antenna_length -4 mm" } });
  for (let i = 1; i <= 8; i++) ev.push({ t: 48.4 + i * 0.7, type: "sim_progress", payload: { job_id: "sim-confirm", pct: i / 8, step: "fine mesh" } });
  ev.push({ t: 54.4, type: "sim_done", payload: { job_id: "sim-confirm", summary: { f0_ghz: 2.431, s11_db: -17.4, bw_mhz: 92 }, curve: "confirm", trust: "ok" } });
  ev.push({ t: 55, type: "finding_upserted", payload: { finding: FINDINGS["F-001"] } });
  ev.push({ t: 55.2, type: "marks_changed", payload: { finding_id: "F-001", marks: FINDINGS["F-001"].marks } });
  ev.push({ t: 55.4, type: "spotlight", payload: { targets: ["net:ANT_FEED"], finding_id: "F-001", reason: "Confirmed: shorten the meander about 4 mm" } });
  ev.push(thought(56, "Confirm run agrees with the screen run within 5 MHz, so the shortened meander is a supported fix."));
  ev.push(budget(56.4, { wall_left_s: 1440, solver_left_s: 710, sims_left: 3, turns_used: 61, images_left: 9 }));

  ev.push(phase(57, 6, "Assemble the report"));
  ev.push(...tool(57.4, "c11", "submit_summary", "Submitted the summary", { dt: 0.6 }));
  ev.push({ t: 58.4, type: "report_ready", payload: { revision: 1 } });
  ev.push({ t: 58.8, type: "completed", payload: { end_state: "complete" } });
  return ev.sort((a, b) => a.t - b.t);
}

export function toEnvelopes(timed: TimedEvent[], reviewId = "r-0042") {
  const base = Date.UTC(2026, 9, 31, 9, 0, 0);
  return timed.map((e, i) => ({
    id: `evt-${i + 1}`,
    sequence: i + 1,
    project_id: "p-nrf52840",
    chat_id: reviewId,
    turn_id: e.turn ?? "turn-1",
    type: e.type,
    payload: e.payload,
    created_at: new Date(base + e.t * 1000).toISOString(),
    _t: e.t,
  }));
}
