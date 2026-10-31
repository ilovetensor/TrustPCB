// Pure helpers for the power, heat and signal simulations: no browser, no React. Unit tested.
import { fmtNum } from "./overlay-math.mjs";

export const BOARD_KINDS = ["power", "thermal", "signal"];
export const KIND_LABEL = { power: "Power", thermal: "Heat", signal: "Signal" };
export const KIND_NOUN = { power: "Power delivery", thermal: "Heat", signal: "Signal integrity" };
export const SOURCE_LABEL = { datasheet: "Datasheet", user: "You", default: "Default", board: "Board" };
export const SOURCE_HINT = {
  datasheet: "Read from the part's datasheet.",
  user: "Set by you or by an instruction you gave.",
  default: "A typical value used because nothing better was known. Replace it to make the number yours.",
  board: "Measured from the board files.",
};

export const isBoardKind = (k) => BOARD_KINDS.includes(k);
/** A row of GET /reviews/{id}/sims that belongs to the board simulations (the RF jobs have kinds base, whatif and confirm). */
export const isBoardRow = (row) => !!row && (isBoardKind(row.sim_kind) || isBoardKind(row.kind));
const kindOfRow = (row) => (isBoardKind(row.sim_kind) ? row.sim_kind : row.kind);

/** One API row as a run the lab can show. Unknown or missing fields stay missing: nothing is filled in. */
export function normaliseRun(row) {
  const status = row.status === "ok" ? "done" : row.status;
  const waiting = row.waiting ?? (row.phase === "waiting" || row.step === "waiting" ? { position: row.position ?? null, ahead: row.ahead ?? null } : null);
  return {
    run_id: String(row.run_id ?? row.job_id ?? row.id),
    kind: kindOfRow(row),
    status,
    label: row.label ?? null,
    ref: row.ref ?? null,
    whatif: Array.isArray(row.whatif) ? row.whatif : Array.isArray(row.whatif_ops) ? row.whatif_ops : [],
    base_run: row.base_run ?? null,
    progress: typeof row.progress === "number" ? row.progress : null,
    step: row.step ?? null,
    waiting: waiting && status !== "done" ? waiting : null,
    result: row.result ?? null,
    evidence: row.evidence ?? null,
    created_at: row.created_at ?? null,
    error: row.error ?? null,
    fixture: !!row.fixture,
  };
}

const num = (v, unit) => (unit === "A" && v < 1 ? `${fmtNum(v * 1000)} mA` : unit === "W" && v < 1 ? `${fmtNum(v * 1000)} mW` : `${fmtNum(v)} ${unit}`);
/** What-if operations in words. */
export function describeBoardOps(ops) {
  return (ops ?? []).map((o) => {
    switch (o.op) {
      case "widen_track": return `Widen ${o.net} to ${fmtNum(o.width_mm)} mm`;
      case "add_via": return `Add a via on ${o.net}`;
      case "add_stitching": return "Add ground stitching vias";
      case "remove_gap": return "Close a gap in the plane";
      case "set_current": return `Set ${o.ref} to ${num(o.amps, "A")}`;
      case "set_part_power": return `Set ${o.ref} to ${num(o.watts, "W")}`;
      default: return String(o.op ?? "change").replace(/_/g, " ");
    }
  }).join(", ");
}

export const isDoneRun = (r) => r.status === "done" && !!r.result;
export const isLiveRun = (r) => r.status === "queued" || r.status === "running";

/** The before and after of a what-if: the base run and the run that changed it. Null when the run has no counterpart. */
export function pairOf(runs, active) {
  if (!active || !isDoneRun(active)) return null;
  const done = runs.filter((r) => isDoneRun(r) && r.kind === active.kind);
  if (active.whatif.length) {
    const before = done.find((r) => r.run_id === active.base_run) ?? [...done].reverse().find((r) => !r.whatif.length);
    return before ? { before, after: active } : null;
  }
  const after = [...done].reverse().find((r) => r.whatif.length && (r.base_run === active.run_id || !r.base_run));
  return after ? { before: active, after } : null;
}

/** The run shown first: the newest finished base run of the asked kind, else any finished one, else any. */
export function pickRun(runs, { kind = null, run = null, ref = null } = {}) {
  if (run) { const f = runs.find((r) => r.run_id === run); if (f) return f; }
  if (ref) { const f = runs.find((r) => r.ref === ref); if (f) return f; }
  const pool = kind ? runs.filter((r) => r.kind === kind) : runs;
  const done = pool.filter(isDoneRun);
  return [...done].reverse().find((r) => !r.whatif.length) ?? done[done.length - 1] ?? pool[pool.length - 1] ?? null;
}

const GROUPS = ["load", "part", "segment", "net"];
/** A result's metrics sorted into what each panel shows: headline figures, then rows by group (load, part, segment, net). */
export function panelModel(result) {
  const out = { headline: [], load: [], part: [], segment: [], net: [], other: [] };
  for (const [key, m] of Object.entries(result?.metrics ?? {})) {
    const prefix = key.split(/[.:]/)[0];
    const group = m.group ?? (key.includes(".") || key.includes(":") ? prefix : null);
    const row = { key, ...m };
    if (group && GROUPS.includes(group)) out[group].push(row);
    else if (group) out.other.push(row);
    else out.headline.push(row);
  }
  return out;
}

/** Series of a result: the ones it lists, plus any a metric carries. */
export function seriesOf(result) {
  const out = [...(result?.series ?? [])];
  for (const [key, m] of Object.entries(result?.metrics ?? {})) if (m.series) out.push({ id: key, label: m.label, ...m.series });
  return out;
}

export const defaultCount = (assumptions) => (assumptions ?? []).filter((a) => a.source === "default").length;

/** The sources of the assumptions that begin with a part's reference: "U3 current" belongs to U3. */
export function sourcesFor(ref, assumptions) {
  if (!ref) return [];
  return (assumptions ?? []).filter((a) => a.name === ref || a.name.startsWith(`${ref} `)).map((a) => a.source);
}
export const usesDefault = (ref, assumptions) => sourcesFor(ref, assumptions).includes("default");

/** The trust verdict of a result: ok only when it says so and no check failed. */
export function trustOf(result) {
  const t = result?.trust;
  const checks = t?.checks ?? [];
  const failed = checks.filter((c) => c.ok === false);
  return { ok: t?.status === "ok" && failed.length === 0, status: t?.status ?? null, checks, failed };
}

/** Share of a limit used, in percent, or null without a limit. */
export const limitShare = (value, limit) => (limit && Number.isFinite(value) ? (value / limit) * 100 : null);

/** Change of a metric between two results, same name; null when either is missing. */
export function metricChange(before, after, key) {
  const a = before?.metrics?.[key], b = after?.metrics?.[key];
  if (!a || !b || typeof a.value !== "number" || typeof b.value !== "number") return null;
  return { before: a.value, after: b.value, delta: b.value - a.value, unit: b.unit };
}

/** What a simulation job is called in a progress list: the three board kinds, else it is an antenna solver job. */
export const simKindName = (kind) => (isBoardKind(kind) ? KIND_LABEL[kind] : "Antenna");

/** The sentence for a job that is queued or running: its place in the line for the shared solver, or its step and share done. */
export function simStatusText(s) {
  if (s.waiting) return `Waiting for the solver${s.waiting.ahead ? `, ${s.waiting.ahead} ahead` : s.waiting.position ? `, place ${s.waiting.position}` : ""}`;
  const pct = typeof s.pct === "number" ? ` ${Math.round(s.pct * 100)}%` : "";
  return `${s.step && s.step !== "queued" ? s.step.charAt(0).toUpperCase() + s.step.slice(1) : s.status === "queued" ? "Queued" : "Solving"}${pct}`;
}

/**
 * Where an evidence row of a finding opens its simulation: a power, heat or signal run in the simulation lab, an antenna job in the RF lab.
 * The row's values name the run; with none named there is no link. Returns null for evidence that is not a simulation.
 */
export function evidenceLink(reviewId, evidence) {
  const v = evidence?.values ?? {};
  const kind = isBoardKind(v.sim_kind) ? v.sim_kind : isBoardKind(v.kind) ? v.kind : isBoardKind(v.sim_kind?.value) ? v.sim_kind.value : null;
  const id = (x) => (typeof x === "string" ? x : typeof x?.value === "string" ? x.value : null);
  const enc = encodeURIComponent;
  const lab = kind || /board_sim|compare_sims/.test(evidence?.source_tool ?? "");
  const run = id(v.run_id) ?? id(v.sim_run);
  if (lab) {
    const q = [kind ? `kind=${kind}` : null, run ? `run=${enc(run)}` : id(v.ref) ? `ref=${enc(id(v.ref))}` : null].filter(Boolean).join("&");
    return { href: `/reviews/${reviewId}/sim${q ? `?${q}` : ""}`, label: "Open in the simulation lab" };
  }
  const job = id(v.job_id);
  if (job) return { href: `/reviews/${reviewId}/rf?job=${enc(job)}`, label: "Open in the RF lab" };
  return null;
}
