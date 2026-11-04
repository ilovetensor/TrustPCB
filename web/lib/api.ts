// The single data-access module for the web app.
// Live by default: every call goes to same-origin /api/... (the Next route handler adds the token).
// Fixture mode is an explicit, opt-in design aid: NEXT_PUBLIC_TRUSTPCB_FIXTURES=1. In live mode
// no fixture module is ever loaded and no invented number can appear.

import { netId } from "./geometry.mjs";
import { runnerOfProfile } from "./agents.mjs";
import { loadFixtures } from "./fixture-loader";
import { apiGet, apiSend, ApiError, uploadFiles } from "./live";
import type { ChangeState, VersionChange } from "./story.mjs";
import type { Board, Evidence, Finding, ProjectSummary, Severity } from "./types";

export { ApiError };
export const FIXTURE_MODE = process.env.NEXT_PUBLIC_TRUSTPCB_FIXTURES === "1";
export const DATA_SOURCE: "fixture" | "live" = FIXTURE_MODE ? "fixture" : "live";
export const FIXTURE_NOTE = "Dev fixture data. Not a real review.";

const fx = loadFixtures;

/* ------------------------------ view models ------------------------------ */

export interface Goal { purpose?: string; band_hz?: [number, number]; target_s11_db?: number; focus?: string }
export type Depth = "quick" | "standard" | "deep";

export interface Detection {
  wireless?: boolean; confidence?: string; ambiguous?: boolean;
  band?: { low_ghz: number; high_ghz: number; label: string; source?: string; confidence?: string } | null;
  antenna?: { type?: string; shape?: string; net?: string; length_mm?: number; layer?: string; in_keepout?: boolean } | null;
  parts?: { ref: string; value: string; family?: string; technology?: string; band_ghz?: [number, number] }[];
  keepouts?: { id: string; name: string; layers: string[]; bbox: number[]; blocks_copper?: boolean }[];
  evidence?: { kind: string; text: string }[];
  [k: string]: unknown;
}
export interface Stackup {
  source?: string; assumed?: boolean; copper_layers?: number; total_thickness_mm?: number; needs_confirmation?: boolean; message?: string;
  layers?: { name: string; kind: string; thickness_mm: number; epsilon_r: number | null; loss_tangent?: number | null; material?: string }[];
  assumptions?: { id: string; text: string; source: string }[];
  warnings?: string[];
}
export interface VersionInfo {
  id: string; project_id: string; number: number; label: string | null; status: string; error: string | null; created_at: string;
  stackup: Stackup | null; detection: Detection | null; duplicate_of?: string | null;
  artifacts?: { id: string; type: string; mime: string; size: number; url: string }[];
}
export interface VersionRow {
  id: string; number: number; label: string; review_id: string | null; at: string; counts: Record<Severity, number> | null;
  status: string; state: ProjectSummary["state"]; note: string; detection: Detection | null; f0_ghz?: number | null;
}
export interface ProjectDetail { project: ProjectSummary; versions: VersionRow[] }
export interface ThreadInfo { id: string; review_id: string; chat_id: string; level: string; status: string; created_at: string }
export interface ReviewMeta {
  id: string; project_id: string; project_name: string; version_label: string; version_id: string; version_number: number;
  profile: string; depth: string; goal: Goal; goal_text: string; started_at: string;
  /** The runner that wrote this review and its follow-ups (the review's stored profile). Missing on old rows and in fixtures: the mark is then neutral. */
  runner?: string | null;
  status: string; end_state: string | null; chat_status: string | null; threads: ThreadInfo[]; report_revision: number | null;
  counts: Record<Severity, number>; rf: { status: string; reason: string | null } | null; fixture?: boolean;
}
export interface SceneInfo { view_box: [number, number, number, number]; layers: string[]; urls: Record<string, string>; px_per_mm?: number }
export interface Zone { role: string; index: number; parts: string[]; ring: [number, number][]; area_mm2: number }
export interface BoardBundle { board: Board; glbUrl: string | null; scene: SceneInfo | null; zones: Zone[]; fixture?: boolean }
export interface RevisionRow { id: string; number: number; cause: string; created_at: string }
export interface Snapshot {
  revision: number; cause: string; incomplete: boolean; created_at: string;
  cover: Record<string, any>; summary: { text: string; top_actions?: string[] } | null;
  findings: Finding[]; evidence: Evidence[]; assumptions: { name: string; value: unknown; source: string; note?: string }[];
  not_tested: string[]; rf: { status: string; reason?: string | null } | null; [k: string]: any;
}

const STATE_OF: Record<string, ProjectSummary["state"]> = {
  complete: "complete", complete_with_limits: "complete_with_limits", agent_stopped: "agent_stopped", canceled: "canceled", pipeline_failed: "failed",
};
function reviewState(r: { status?: string; end_state?: string | null } | null | undefined): ProjectSummary["state"] {
  if (!r) return "no_review";
  if (r.status === "queued") return "queued";
  if (r.status === "running") return "running";
  if (r.status === "interrupted") return "interrupted";
  return STATE_OF[r.end_state ?? ""] ?? (r.status === "failed" ? "failed" : r.status === "canceled" ? "canceled" : "complete");
}
const ZERO: Record<Severity, number> = { critical: 0, high: 0, medium: 0, low: 0, info: 0 };

function kindOf(d: Detection | null | undefined): string {
  if (!d) return "Board";
  if (d.wireless) return d.band?.label ? `Wireless board, ${d.band.label}` : "Wireless board";
  return "Board without a radio";
}

/* ------------------------------ projects ------------------------------ */

function summariseProject(p: any): ProjectSummary {
  const versions: any[] = p.versions ?? [];
  const last = p.dashboard?.last_review ?? null;
  const latestVersion = versions[versions.length - 1];
  const counts = (last?.counts ?? ZERO) as Record<Severity, number>;
  const wireless = versions.map((v) => v.detection?.wireless).find((w) => w !== undefined && w !== null);
  return {
    id: p.id, name: p.name, kind: kindOf(latestVersion?.detection), updated_at: last?.updated_at ?? latestVersion?.created_at ?? p.created_at,
    versions: versions.length, latest_version: latestVersion ? (latestVersion.label || `v${latestVersion.number}`) : "none", latest_review_id: last?.id ?? "",
    counts, rf: wireless ? "not_tested" : wireless === false ? "n/a" : "not_tested", state: reviewState(last),
    trend: (p.dashboard?.trend ?? []).map((t: any) => Object.values(t.counts as Record<string, number>).reduce((a, b) => a + b, 0)),
  };
}

export async function listProjects(): Promise<ProjectSummary[]> {
  if (FIXTURE_MODE) return (await fx()).listProjects();
  const list = await apiGet<any[]>("projects");
  const full = await Promise.all(list.map((p) => apiGet<any>(`projects/${p.id}`).catch(() => ({ ...p, versions: [] }))));
  const rows = full.map(summariseProject);
  // the RF column comes from the stored confirm result of the latest review, not from a guess
  await Promise.all(rows.map(async (row) => {
    if (row.rf !== "not_tested" || !row.latest_review_id) return;
    const r = await getVersionRf(row.latest_review_id);
    if (r?.status === "tested" && r.worst_s11_db != null) {
      row.rf = r.meets_target ? "pass" : "fail";
      const fmt = (v: number) => v.toFixed(2).replace(/\.?0+$/, "").replace("-", "\u2212");
      row.rf_note = `${r.accepted_worst_pct != null ? `${r.accepted_worst_pct} % accepted at worst` : `${fmt(r.worst_s11_db)} dB at worst`}, target ${r.meets_target ? "met" : "not met"}`;
    }
  }));
  return rows.sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
}

export async function getProject(id: string): Promise<ProjectDetail | null> {
  if (FIXTURE_MODE) return (await fx()).getProject(id);
  let p: any;
  try { p = await apiGet<any>(`projects/${id}`); } catch (e) { if (e instanceof ApiError && e.status === 404) return null; throw e; }
  const versions: VersionRow[] = (p.versions as any[]).map((v) => {
    const review = v.reviews?.[v.reviews.length - 1] ?? null;
    return {
      id: v.id, number: v.number, label: v.label || `v${v.number}`, review_id: review?.id ?? null, at: v.created_at,
      counts: review ? review.counts : null, status: v.status, state: v.status === "failed" ? "failed" : reviewState(review),
      note: v.status === "failed" ? v.error ?? "Upload failed" : review ? "" : "No review yet", detection: v.detection,
    };
  });
  return { project: summariseProject(p), versions };
}

export async function createProject(name: string): Promise<{ id: string; name: string }> {
  if (FIXTURE_MODE) throw new ApiError(0, "Uploads are off in fixture mode.");
  return apiSend("POST", "projects", { name });
}
export async function uploadVersion(projectId: string, files: File[], label?: string, onProgress?: (f: number) => void): Promise<VersionInfo> {
  if (FIXTURE_MODE) throw new ApiError(0, "Uploads are off in fixture mode.");
  return uploadFiles(`projects/${projectId}/versions`, files, label ? { label } : {}, onProgress);
}
export async function getVersion(id: string): Promise<VersionInfo> {
  return apiGet(`versions/${id}`);
}
/** `extent` is the list of checks chosen on the start form (layout is always in it). Left out, the server uses its own default. */
export async function startReview(versionId: string, goal: Goal, depth: Depth, extent?: string[]): Promise<{ id: string }> {
  if (FIXTURE_MODE) throw new ApiError(0, "Reviews are off in fixture mode.");
  return apiSend("POST", `versions/${versionId}/reviews`, extent ? { goal, depth, extent } : { goal, depth });
}
/** Which API keys are set: {NAME: {set, source}}. Values are never sent. Null when the settings route is not there or could not be read.
 *  The server answers {read_only, keys: [{name, service, label, get_url, set, source}], parts_available}. */
export async function getKeyStatus(): Promise<Record<string, { set: boolean; source?: string }> | null> {
  if (FIXTURE_MODE) return null;
  try {
    const r = await apiGet<Record<string, any>>("settings/keys");
    const list = Array.isArray(r?.keys) ? r.keys as { name: string; set: boolean; source?: string | null }[] : null;
    if (!list) return null;
    return Object.fromEntries(list.map((k) => [k.name, { set: !!k.set, source: k.source ?? undefined }]));
  } catch { return null; }
}

/** Whether a Muse key (Meta or Baseten, from Settings or the environment) is set; null when unknown (fixtures, demo, offline). */
export async function getModelAvailable(): Promise<boolean | null> {
  if (FIXTURE_MODE) return null;
  try {
    const r = await apiGet<Record<string, any>>("settings/keys");
    return r?.read_only || typeof r?.model_available !== "boolean" ? null : r.model_available;
  } catch { return null; }
}

/* ------------------------------ board ------------------------------ */

const boardCache = new Map<string, Promise<BoardBundle>>();

/** The server writes net names bare on tracks and pads; target ids and marks use `net:<name>`. */
export function normaliseBoard(b: Board): Board {
  const fix = <T extends { net: string | null }>(xs: T[]) => xs.map((x) => (x.net != null && !x.net.startsWith("net:") ? { ...x, net: netId(x.net) } : x));
  return { ...b, pads: fix(b.pads), tracks: fix(b.tracks), vias: fix(b.vias), zones: fix(b.zones) as Board["zones"] };
}

export function getBoardBundle(versionId: string): Promise<BoardBundle> {
  if (FIXTURE_MODE) return fx().then((m) => m.getBoardBundle());
  let p = boardCache.get(versionId);
  if (!p) {
    p = (async () => {
      const v = await getVersion(versionId);
      const by = new Map((v.artifacts ?? []).map((a) => [a.type, a]));
      const boardArt = by.get("board.json");
      if (!boardArt) throw new ApiError(404, "This version has no board.json yet.");
      const board = normaliseBoard(await (await fetch(boardArt.url)).json());
      let scene: SceneInfo | null = null;
      const sceneArt = by.get("scene/scene.json");
      if (sceneArt) {
        const meta = await (await fetch(sceneArt.url)).json();
        const urls: Record<string, string> = {};
        for (const name of meta.layers as string[]) {
          const art = by.get(`scene/${name}.svg`);
          if (art) urls[name] = art.url;
        }
        scene = { view_box: meta.view_box, layers: meta.layers, urls, px_per_mm: meta.px_per_mm };
      }
      let zones: Zone[] = [];
      const anaArt = by.get("analyzers.json");
      if (anaArt) {
        try {
          const ana = await (await fetch(anaArt.url)).json();
          zones = (ana?.overlays?.zones?.zones ?? []) as Zone[];
        } catch { /* optional */ }
      }
      return { board, glbUrl: by.get("board.glb")?.url ?? null, scene, zones };
    })();
    boardCache.set(versionId, p);
    p.catch(() => boardCache.delete(versionId));
  }
  return p;
}

/* ------------------------------ review ------------------------------ */

function goalText(g: Goal | null | undefined): string {
  if (!g) return "";
  return [g.purpose, g.focus].filter(Boolean).join(". ");
}

export async function getReview(id: string): Promise<ReviewMeta> {
  if (FIXTURE_MODE) return (await fx()).getReview(id);
  const r = await apiGet<any>(`reviews/${id}`);
  const [project, version] = await Promise.all([apiGet<any>(`projects/${r.project_id}`), getVersion(r.version_id)]);
  return {
    id: r.id, project_id: r.project_id, project_name: project.name, version_id: r.version_id, version_number: version.number,
    version_label: version.label || `v${version.number}`, profile: r.profile?.name ?? "agent", runner: runnerOfProfile(r.profile), depth: r.depth, goal: r.goal ?? {}, goal_text: goalText(r.goal),
    started_at: r.created_at, status: r.status, end_state: r.end_state, chat_status: r.chat_status, threads: r.threads ?? [],
    report_revision: r.report_revision, counts: r.counts, rf: r.summary?.rf ? { status: r.summary.rf.simulatable ? "available" : "not_tested", reason: r.summary.rf.reason ?? null } : null,
  };
}

export async function listFindings(reviewId: string): Promise<Finding[]> {
  if (FIXTURE_MODE) return (await fx()).listFindings(reviewId);
  return apiGet(`reviews/${reviewId}/findings`);
}
export async function patchFinding(id: string, status: "dismissed" | "open", reason?: string): Promise<Finding> {
  return apiSend("PATCH", `findings/${id}`, { status, reason });
}
export async function cancelReview(reviewId: string) { return apiSend<any>("POST", `reviews/${reviewId}/cancel`); }
export async function cancelThread(threadId: string) { return apiSend<any>("POST", `threads/${threadId}/cancel`); }
export async function startThread(reviewId: string, level?: string): Promise<ThreadInfo> {
  return apiSend("POST", `reviews/${reviewId}/threads`, level ? { level } : {});
}
export async function sendThreadMessage(threadId: string, text: string, selected: { id: string; type?: string }[], requestId: string) {
  return apiSend<any>("POST", `threads/${threadId}/messages`, { text, selected, request_id: requestId });
}
export async function respondRequest(requestId: string, body: { allow?: boolean; answers?: Record<string, string> }) {
  return apiSend<any>("POST", `requests/${requestId}/respond`, body);
}

/* ------------------------------ reports ------------------------------ */

export async function listRevisions(reviewId: string): Promise<RevisionRow[]> {
  if (FIXTURE_MODE) return [];
  return apiGet(`reviews/${reviewId}/report/revisions`);
}
export async function getSnapshot(reviewId: string, revision: number): Promise<Snapshot> {
  return apiGet(`reviews/${reviewId}/report/${revision}.json`);
}
export const reportUrl = (reviewId: string, revision: number, fmt: "html" | "pdf" | "json" = "html") => `/api/reviews/${reviewId}/report/${revision}.${fmt}`;

/** Evidence rows by display code (E-007), from the latest report snapshot. Empty while no report exists. */
export async function getEvidenceByCode(reviewId: string): Promise<Record<string, Evidence>> {
  if (FIXTURE_MODE) return (await fx()).getEvidenceByCode();
  try {
    const rows = await listRevisions(reviewId);
    if (!rows.length) return {};
    const snap = await getSnapshot(reviewId, rows[rows.length - 1].number);
    return Object.fromEntries((snap.evidence ?? []).map((e) => [e.code ?? e.id, e]));
  } catch { return {}; }
}

/* ------------------------------ RF result per version ------------------------------ */

export interface VersionRf {
  status: "tested" | "screen_only" | "not_tested"; reason: string | null;
  worst_s11_db: number | null; mean_s11_db: number | null; accepted_worst_pct: number | null; accepted_mean_pct: number | null;
  target_db: number | null; meets_target: boolean | null; evidence: string | null;
}
const num = (m: any, ...names: string[]): number | null => { for (const n of names) { const v = m?.[n]?.value; if (typeof v === "number") return v; } return null; };

/** The confirmed as-built result of a review, read from its latest report snapshot. Nothing is estimated: no confirmed base means no figure. */
export async function getVersionRf(reviewId: string): Promise<VersionRf | null> {
  if (FIXTURE_MODE) return null;
  try {
    const rows = await listRevisions(reviewId);
    if (!rows.length) return null;
    const rf = (await getSnapshot(reviewId, rows[rows.length - 1].number)).rf as any;
    if (!rf || rf.status !== "tested") return { status: "not_tested", reason: rf?.reason ?? null, worst_s11_db: null, mean_s11_db: null, accepted_worst_pct: null, accepted_mean_pct: null, target_db: rf?.target_db ?? null, meets_target: null, evidence: null };
    const base = rf.confirm_base;
    if (!base) return { status: "screen_only", reason: "Only screen runs exist: they compare changes and give no absolute result.", worst_s11_db: null, mean_s11_db: null, accepted_worst_pct: null, accepted_mean_pct: null, target_db: rf.target_db ?? null, meets_target: null, evidence: null };
    const m = base.measurements ?? {};
    const worst = num(m, "as_built_band_worst_s11", "s11_band_worst");
    return {
      status: "tested", reason: null, worst_s11_db: worst, mean_s11_db: num(m, "as_built_band_mean_s11", "s11_band_mean"),
      accepted_worst_pct: num(m, "as_built_band_worst_accepted_power_pct", "band_worst_accepted_power_pct"), accepted_mean_pct: num(m, "as_built_band_mean_accepted_power_pct", "band_mean_accepted_power_pct"),
      target_db: rf.target_db ?? null, meets_target: worst != null && typeof rf.target_db === "number" ? worst <= rf.target_db : null, evidence: base.code ?? null,
    };
  } catch { return null; }
}

/* ------------------------------ compare ------------------------------ */

export interface FindingBrief { id: string; code: string; fingerprint: string; title: string; severity: Severity; class: string; status: string; dismissed_reason?: string | null; was?: FindingBrief | null; changes?: string[]; dismissed_on?: FindingBrief; change?: ChangeState | "not_rechecked"; note?: string | null; matched_by?: string; unrecorded?: boolean }
export interface BoardDiff {
  parts: { added: { ref: string; value: string }[]; removed: { ref: string; value: string }[]; moved: { ref: string; from: [number, number]; to: [number, number]; distance_mm: number }[]; value_changed: { ref: string; from: string; to: string }[] };
  tracks: { added: number; removed: number; length_added_mm: number; length_removed_mm: number };
  vias: { added: number; removed: number; count_old: number; count_new: number };
  nets: { added: string[]; removed: string[] }; zones: { old: number; new: number };
}
export interface CompareResult {
  from: { version_id: string; number: number; review_id: string | null; counts: Record<Severity, number> | null };
  to: { version_id: string; number: number; review_id: string | null; counts: Record<Severity, number> | null };
  findings: { fixed: FindingBrief[]; still_present: FindingBrief[]; changed: FindingBrief[]; new: FindingBrief[]; dismissed_carried: FindingBrief[]; not_rechecked: FindingBrief[];
              counts?: Record<ChangeState | "dismissed_carried" | "not_rechecked", number> } | null;
  /** The as-built result against the older version, with the older review's prediction. Null when either version has no confirmed antenna base. */
  version_change?: VersionChange | null;
  board: BoardDiff | null; counts_change?: Record<string, number>; notes: string[];
}
export async function getCompare(from: string, to: string): Promise<CompareResult> {
  return apiGet(`compare?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);
}

/* ------------------------------ settings: optional service keys ------------------------------ */

export interface KeyRow { name: string; service: string; label: string; get_url: string; set: boolean; source: "env" | "app" | null }
export interface KeySettings { read_only: boolean; keys: KeyRow[]; parts_available: boolean; model_available?: boolean }
let fixtureKeys: KeyRow[] = [
  { name: "META_API_KEY", service: "Meta", label: "Meta API key (Muse Spark on api.meta.ai)", get_url: "https://developer.meta.ai/", set: false, source: null },
  { name: "BASETEN_API_KEY", service: "Baseten", label: "Baseten API key (Muse Spark Open)", get_url: "https://app.baseten.co/settings/api_keys", set: false, source: null },
  { name: "DIGIKEY_CLIENT_ID", service: "DigiKey", label: "DigiKey client ID", get_url: "https://developer.digikey.com/", set: false, source: null },
  { name: "DIGIKEY_CLIENT_SECRET", service: "DigiKey", label: "DigiKey client secret", get_url: "https://developer.digikey.com/", set: false, source: null },
  { name: "MOUSER_API_KEY", service: "Mouser", label: "Mouser search API key", get_url: "https://www.mouser.com/api-hub/", set: false, source: null },
];
const fixtureSettings = (): KeySettings => ({ read_only: false, keys: fixtureKeys, parts_available: fixtureKeys.some((k) => k.set) });
/** What is set, never the value: the backend returns only set and source. */
export async function getKeySettings(): Promise<KeySettings> {
  if (FIXTURE_MODE) return fixtureSettings();
  return apiGet("settings/keys");
}
/** The value goes out once and is never returned. */
export async function saveKey(name: string, value: string): Promise<KeySettings> {
  if (FIXTURE_MODE) { fixtureKeys = fixtureKeys.map((k) => (k.name === name ? { ...k, set: true, source: "app" } : k)); return fixtureSettings(); }
  const r = await apiSend<{ keys: KeyRow[] }>("PUT", `settings/keys/${name}`, { value });
  return getKeySettings().catch(() => ({ read_only: false, keys: r.keys, parts_available: r.keys.some((k) => k.set) }));
}
export async function removeKey(name: string): Promise<KeySettings> {
  if (FIXTURE_MODE) { fixtureKeys = fixtureKeys.map((k) => (k.name === name ? { ...k, set: false, source: null } : k)); return fixtureSettings(); }
  await apiSend("DELETE", `settings/keys/${name}`);
  return getKeySettings();
}
