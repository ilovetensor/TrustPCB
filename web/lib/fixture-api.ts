// Dev-only fixture implementation. Reached only when NEXT_PUBLIC_TRUSTPCB_FIXTURES=1 (see lib/api.ts).
// Every number here is an illustrative placeholder for layout work. None of it may reach live mode.

import type { Board, EventEnvelope, Finding, Evidence, ProjectSummary } from "./types";
import { PROJECTS, VERSIONS, COMPARE_NRF } from "./fixtures/projects";
import {
  ASSUMPTIONS, buildReplay, CURVES, EVIDENCE, FINDINGS, NOT_TESTED, NRF_ASSETS, TARGET_BAND, toEnvelopes, type Curve,
} from "./fixtures/nrf-review";
import type { BoardBundle, ProjectDetail, ReviewMeta } from "./api";

export const FIXTURE_NOTE = "Dev fixture data. Not a real review.";

export type { Curve };
export { TARGET_BAND };

/* ------------------------------ projects ------------------------------ */

export async function listProjects(): Promise<ProjectSummary[]> {
  return PROJECTS;
}
export async function getProject(id: string): Promise<ProjectDetail | null> {
  const project = PROJECTS.find((p) => p.id === id);
  if (!project) return null;
  return {
    project,
    versions: (VERSIONS[id] ?? []).map((v, i) => ({
      id: v.id, number: i + 1, label: v.label, review_id: v.review_id, at: v.at, counts: v.counts, status: "ready",
      state: "complete" as const, note: v.note, detection: null, f0_ghz: v.f0_ghz,
    })),
  };
}
export function findProjectForReview(reviewId: string): ProjectSummary {
  return PROJECTS.find((p) => p.latest_review_id === reviewId) ?? PROJECTS[0];
}

/* ------------------------------ board ------------------------------ */

let boardCache: Promise<Board> | null = null;
export async function getBoard(_versionId?: string): Promise<Board> {
  boardCache ??= fetch(NRF_ASSETS.board).then((r) => r.json());
  return boardCache;
}
export async function getBoardBundle(): Promise<BoardBundle> {
  const board = await getBoard();
  return { board, glbUrl: NRF_ASSETS.glb, scene: null, zones: [], fixture: true };
}

/* ------------------------------ review ------------------------------ */

export async function getReview(id: string): Promise<ReviewMeta> {
  const project = findProjectForReview(id);
  return {
    id, project_id: project.id, project_name: project.name, version_label: project.latest_version, version_id: "ver-2", version_number: 2,
    profile: "Fixture replay", depth: "standard", goal: {}, goal_text: "BLE node on 2.4 GHz. Is the antenna OK?", started_at: "2026-10-31T09:00:00Z",
    status: "completed", end_state: "complete", chat_status: "completed", threads: [], report_revision: 1,
    counts: project.counts, rf: null, fixture: true,
  };
}

export interface ReplaySource {
  events: (EventEnvelope & { _t: number })[];
  duration: number;
  fixture: boolean;
}

const MEAS = (s: { f0_ghz: number; s11_db: number; bw_mhz: number }) => ({
  f_res_hz: { value: s.f0_ghz * 1e9, unit: "Hz" }, s11_min_db: { value: s.s11_db, unit: "dB" }, bandwidth_hz: { value: s.bw_mhz * 1e6, unit: "Hz" },
});

/** Map the old fixture payloads onto what the live backend emits, so one reducer serves both. */
function adapt(e: EventEnvelope & { _t: number }): EventEnvelope & { _t: number } {
  const p = e.payload as Record<string, any>;
  let type = e.type;
  let payload: Record<string, any> = p;
  switch (e.type) {
    case "status": payload = { state: "running" }; break;
    case "phase_changed": payload = { phase: p.phase, name: p.label, label: p.label }; break;
    case "budget":
      payload = {
        wall_s: { used: p.wall_total_s - p.wall_left_s, limit: p.wall_total_s, left: p.wall_left_s },
        solver_s: { used: p.solver_total_s - p.solver_left_s, limit: p.solver_total_s, left: p.solver_left_s },
        turns: { used: p.turns_used, limit: p.turns_total },
        images: { used: p.images_total - p.images_left, limit: p.images_total },
      };
      break;
    case "tool_start": payload = { tool_call_id: p.call_id, name: p.tool, trail: p.summary, input: {} }; break;
    case "tool_result": payload = { tool_call_id: p.call_id, name: p.tool, content: [{ type: "text", text: p.result ?? "" }] }; break;
    case "candidate_batch": payload = { total: p.total, counts: { new: p.total - p.reviewed } }; break;
    case "brief_ready":
      payload = { brief: { purpose: p.purpose, priorities: p.priorities }, zones: Object.fromEntries(p.zones.map((z: any) => [z.label, [z.target]])) };
      break;
    case "finding_upserted": payload = { finding: { ...p.finding, code: p.finding.id, evidence: p.finding.evidence_ids } }; break;
    case "marks_changed": payload = { finding_id: p.finding_id, code: p.finding_id, marks: p.marks }; break;
    case "spotlight": payload = { targets: p.targets, finding_code: p.finding_id, label: p.reason, resolved: [] }; break;
    case "investigation_updated":
      payload = { investigation_id: p.id, code: "I-001", question: p.question, status: p.status, suspects: p.suspects.map((s: any) => ({ code: s.id, title: s.title, status: s.status })) };
      break;
    case "suspect_updated": payload = { suspect_id: p.suspect_id, code: p.suspect_id, investigation_id: p.investigation_id, status: p.status, reason: p.conclusion }; break;
    case "sim_queued": payload = { ref: p.job_id, hash: p.job_id, label: p.label, kind: p.kind, quality: p.quality, suspect: null }; break;
    case "sim_progress": payload = { ref: p.job_id, fraction: p.pct, phase: p.step }; break;
    case "sim_done": payload = { ref: p.job_id, label: undefined, ok: true, trust: { status: p.trust }, measurements: MEAS(p.summary), curve: (CURVES as Record<string, Curve>)[p.curve] ?? null, quality: "screen" }; break;
    default: break;
  }
  return { ...e, type, payload };
}

export async function getReviewReplay(id: string): Promise<ReplaySource> {
  const events = toEnvelopes(buildReplay(), id).map(adapt);
  return { events, duration: events[events.length - 1]._t, fixture: true };
}

export async function getEvidenceByCode(): Promise<Record<string, Evidence>> {
  return EVIDENCE;
}

export function getFinding(id: string): Finding | undefined {
  return FINDINGS[id];
}
export async function listFindings(_reviewId: string): Promise<Finding[]> {
  return Object.values(FINDINGS);
}
export function getEvidence(ids: string[]): Evidence[] {
  return ids.map((i) => EVIDENCE[i]).filter(Boolean);
}
export async function listAssumptions(_reviewId: string) {
  return { assumptions: ASSUMPTIONS, not_tested: NOT_TESTED };
}

export function resolveCurve(c: string | Curve | undefined | null): Curve | null {
  if (!c) return null;
  if (typeof c !== "string") return c;
  return (CURVES as Record<string, Curve>)[c] ?? null;
}

/* ------------------------------ compare ------------------------------ */

export async function getCompare(_from: string, _to: string) {
  return {
    a: { label: "v1", review_id: "r-0031", curve: CURVES.v1, f0_ghz: 2.285 },
    b: { label: "v2", review_id: "r-0042", curve: CURVES.base, f0_ghz: 2.312 },
    rows: COMPARE_NRF,
    fixture: true,
  };
}

/* ------------------------------ report ------------------------------ */

export async function getReport(reviewId: string) {
  const review = await getReview(reviewId);
  return {
    review,
    revision: 1,
    generated_at: "2026-10-31T09:01:00Z",
    summary:
      "The board has one high risk: the meander antenna resonates at 2.31 GHz, 120 MHz below the band. A supported fix is to shorten the meander by about 4 mm. The remaining findings are routing and placement points that do not stop the board from working.",
    actions: [
      "Shorten the antenna meander by about 4 mm and re-tune the match",
      "Pull the ground pour back from the feed line to at least 1.0 mm",
      "Reroute the 3V3 branch out from under the RF section",
    ],
    findings: Object.values(FINDINGS),
    evidence: EVIDENCE,
    assumptions: ASSUMPTIONS,
    not_tested: NOT_TESTED,
    method: [
      ["KiCad", "10.0.5 kicad-cli, default DRC with board rules"],
      ["Analyzers", "9 rules, no part roles supplied"],
      ["Solver", "openEMS, screen and confirm quality (fixture values)"],
      ["Scenario hash", "fixture-0000"],
    ] as [string, string][],
    fixture: true,
  };
}

/* ------------------------------ follow-up (fixture runner) ------------------------------ */

export interface FollowupChunk { delay: number; type: string; payload: Record<string, any> }
/** Deterministic scripted reply. The live version posts to /api/threads/{id}/messages. */
export function askFollowup(text: string, selected: string[]): FollowupChunk[] {
  const lower = text.toLowerCase();
  const chunks: FollowupChunk[] = [];
  const say = (s: string, d = 40) => s.split(/(?<= )/).forEach((w) => chunks.push({ delay: d, type: "text_delta", payload: { text: w } }));
  if (/simulat|test|rogers|run/.test(lower)) {
    chunks.push({ delay: 300, type: "followup_started", payload: { level: "Test", selected } });
    say("That needs a simulation. I can compare the board with the pour cleared near the feed. It uses the solver, so I need your approval first.");
    chunks.push({ delay: 400, type: "test", payload: { request_id: "req-fx-1", title: "Clear the ground pour 3 mm around the feed line, screen quality", why: "Check how much of the 120 MHz offset the pour explains", estimated_seconds: 240 } });
  } else if (/c12|move|place/.test(lower)) {
    chunks.push({ delay: 300, type: "followup_started", payload: { level: "Check", selected } });
    say("Placing C12 at 3 mm from U3:W1 shortens the supply path by 4.7 mm and keeps it clear of the 0.5 mm track. The effect on the antenna is Not tested in this version.");
    chunks.push({ delay: 300, type: "whatif_ready", payload: { ref: "C12", new_position: [35.6, 25.9], label: "What-if, not your board" } });
  } else {
    chunks.push({ delay: 300, type: "followup_started", payload: { level: "Answer", selected } });
    say(selected.length ? `About ${selected.join(", ")}: it was recorded from measured board geometry, so the number is exact. Open the evidence list on the card for the tool and values.` : "I answer from the saved findings and evidence. Select a part, track or finding on the board to ask about that exact item.");
  }
  chunks.push({ delay: 200, type: "completed", payload: {} });
  return chunks;
}
