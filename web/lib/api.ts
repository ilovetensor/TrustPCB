// The single data-access module for the web app.
// Everything the UI shows comes through here. Today it serves dev fixtures;
// swap the bodies for fetch("/api/...") calls (same-origin proxy) when the
// backend is live. Screens never import fixtures directly.

import type { Board, EventEnvelope, Finding, Evidence, ProjectSummary } from "./types";
import { PROJECTS, VERSIONS, COMPARE_NRF, type VersionRow } from "./fixtures/projects";
import {
  ASSUMPTIONS, buildReplay, CURVES, EVIDENCE, FINDINGS, NOT_TESTED, NRF_ASSETS, TARGET_BAND, toEnvelopes, type Curve,
} from "./fixtures/nrf-review";

export const DATA_SOURCE: "fixture" | "live" = "fixture";
export const FIXTURE_NOTE = "Dev fixture data. Not a real review.";

export type { Curve };
export { TARGET_BAND };

/* ------------------------------ projects ------------------------------ */

export async function listProjects(): Promise<ProjectSummary[]> {
  return PROJECTS;
}
export async function getProject(id: string): Promise<{ project: ProjectSummary; versions: VersionRow[] } | null> {
  const project = PROJECTS.find((p) => p.id === id);
  if (!project) return null;
  return { project, versions: VERSIONS[id] ?? [] };
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
export function getBoardGlbUrl(_versionId?: string): string {
  return NRF_ASSETS.glb;
}

/* ------------------------------ review ------------------------------ */

export interface ReviewMeta {
  id: string; project_id: string; project_name: string; version_label: string; version_id: string;
  profile: string; depth: "Quick" | "Standard" | "Deep"; goal: string; started_at: string;
}
export async function getReview(id: string): Promise<ReviewMeta> {
  const project = findProjectForReview(id);
  return {
    id, project_id: project.id, project_name: project.name, version_label: project.latest_version, version_id: "ver-2",
    profile: "Muse (fixture replay)", depth: "Standard", goal: "BLE node on 2.4 GHz. Is the antenna OK?", started_at: "2026-10-31T09:00:00Z",
  };
}

export interface ReplaySource {
  events: (EventEnvelope & { _t: number })[];
  duration: number;
  fixture: boolean;
}
export async function getReviewReplay(id: string): Promise<ReplaySource> {
  const events = toEnvelopes(buildReplay(), id);
  return { events, duration: events[events.length - 1]._t, fixture: true };
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
    chunks.push({ delay: 400, type: "test_proposed", payload: { request_id: "req-fx-1", what: "Clear the ground pour 3 mm around the feed line, screen quality", why: "Check how much of the 120 MHz offset the pour explains", eta_s: 240 } });
  } else if (/c12|move|place/.test(lower)) {
    chunks.push({ delay: 300, type: "followup_started", payload: { level: "Check", selected } });
    say("Placing C12 at 3 mm from U3:W1 shortens the supply path by 4.7 mm and keeps it clear of the 0.5 mm track. The effect on the antenna is Not tested in this version.");
    chunks.push({ delay: 300, type: "whatif_ready", payload: { ref: "C12", to: [35.6, 25.9], label: "What-if, not your board" } });
  } else {
    chunks.push({ delay: 300, type: "followup_started", payload: { level: "Answer", selected } });
    say(selected.length ? `About ${selected.join(", ")}: it was recorded from measured board geometry, so the number is exact. Open the evidence list on the card for the tool and values.` : "I answer from the saved findings and evidence. Select a part, track or finding on the board to ask about that exact item.");
  }
  chunks.push({ delay: 200, type: "followup_done", payload: {} });
  return chunks;
}
