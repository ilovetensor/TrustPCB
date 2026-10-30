// Client reducer over review events (spec sections 14 and 17).
// Pure and deterministic so a replay, an SSE stream and a page reload agree.

export const PHASES = [
  { id: 0, key: "intake", label: "Intake", short: "INTAKE", actor: "code" },
  { id: 1, key: "baseline", label: "Baseline", short: "BASELINE", actor: "code" },
  { id: 2, key: "understand", label: "Understand", short: "UNDERSTAND", actor: "agent" },
  { id: 3, key: "review", label: "Review", short: "REVIEW", actor: "agent" },
  { id: 4, key: "investigate", label: "Investigate", short: "INVESTIGATE", actor: "agent" },
  { id: 5, key: "confirm", label: "Confirm", short: "CONFIRM", actor: "agent" },
  { id: 6, key: "report", label: "Report", short: "REPORT", actor: "code" },
];

export function initialReviewState() {
  return {
    status: "idle", // idle | running | complete | failed | canceled
    endState: null,
    phase: -1,
    seen: -1,
    budget: null,
    trail: [],
    findings: {},
    findingOrder: [],
    marks: [], // loose marks not tied to a finding (marks_changed with finding_id null)
    brief: null,
    candidates: { total: 0, reviewed: 0, byRule: {} },
    investigations: {},
    investigationOrder: [],
    sims: {},
    experiments: [],
    spotlight: null,
    reportReady: null,
    assumptions: [],
    followup: { level: null, selected: [], messages: [], proposal: null, whatif: null, running: false },
    notices: [],
  };
}

function upsertTrail(state, entry) {
  const i = state.trail.findIndex((e) => e.id === entry.id);
  if (i < 0) return { ...state, trail: [...state.trail, entry] };
  const trail = state.trail.slice();
  trail[i] = { ...trail[i], ...entry };
  return { ...state, trail };
}

function addFinding(state, finding) {
  const exists = !!state.findings[finding.id];
  return {
    ...state,
    findings: { ...state.findings, [finding.id]: { ...(state.findings[finding.id] ?? {}), ...finding } },
    findingOrder: exists ? state.findingOrder : [...state.findingOrder, finding.id],
  };
}

const TOOL_LABELS = {
  get_board_summary: "Read board summary",
  view_board: "Looked at the board",
  read_schematic: "Read schematic",
  list_candidates: "Listed analyzer candidates",
  record_finding: "Recorded a finding",
  show_on_board: "Pointed at the board",
  open_investigation: "Opened an investigation",
  run_simulation: "Ran a simulation",
  view_result: "Viewed a result",
  compare_results: "Compared results",
  tune_matching: "Calculated matching",
  conclude_suspect: "Concluded a suspect",
  confirm_result: "Confirmation run",
  submit_board_brief: "Submitted the board brief",
  submit_summary: "Submitted the summary",
  run_analyzer: "Ran an analyzer",
  measure: "Measured",
  try_placement: "Tried a placement",
};

export function toolLabel(tool) {
  return TOOL_LABELS[tool] ?? tool.replace(/_/g, " ");
}

export function reviewReducer(state, event) {
  if (event.sequence <= state.seen) return state;
  const p = event.payload ?? {};
  let next = { ...state, seen: event.sequence };
  const at = event.created_at;
  switch (event.type) {
    case "status": {
      const s = p.status;
      return { ...next, status: s === "running" ? "running" : next.status };
    }
    case "phase_changed": {
      next = { ...next, status: "running", phase: p.phase };
      return upsertTrail(next, { id: `phase-${p.phase}-${event.sequence}`, kind: "phase", phase: p.phase, text: p.label ?? PHASES[p.phase]?.label ?? "", at, seq: event.sequence });
    }
    case "budget":
      return { ...next, budget: { ...(next.budget ?? {}), ...p } };
    case "thought":
      return upsertTrail(next, { id: `th-${event.sequence}`, kind: "thought", text: p.text, phase: p.phase ?? next.phase, at, seq: event.sequence });
    case "tool_start":
      return upsertTrail(next, {
        id: `tool-${p.call_id ?? event.sequence}`, kind: "tool", tool: p.tool, text: p.summary ?? toolLabel(p.tool),
        state: "running", phase: next.phase, at, seq: event.sequence,
      });
    case "tool_result":
      return upsertTrail(next, {
        id: `tool-${p.call_id ?? event.sequence}`, kind: "tool", tool: p.tool, text: p.summary ?? toolLabel(p.tool),
        state: p.ok === false ? "error" : "done", result: p.result, phase: next.phase, at, seq: event.sequence,
      });
    case "candidate_batch": {
      const c = next.candidates;
      return {
        ...next,
        candidates: {
          total: p.total ?? c.total,
          reviewed: p.reviewed ?? c.reviewed,
          byRule: { ...c.byRule, ...(p.by_rule ?? {}) },
        },
      };
    }
    case "brief_ready":
      return upsertTrail({ ...next, brief: p }, { id: `brief-${event.sequence}`, kind: "result", text: "Board brief ready", detail: p.purpose, at, seq: event.sequence });
    case "finding_upserted": {
      const f = p.finding;
      next = addFinding(next, f);
      return upsertTrail(next, { id: `find-${f.id}`, kind: "finding", findingId: f.id, text: f.title, severity: f.severity, at, seq: event.sequence });
    }
    case "marks_changed": {
      if (p.finding_id && next.findings[p.finding_id])
        return { ...next, findings: { ...next.findings, [p.finding_id]: { ...next.findings[p.finding_id], marks: p.marks } } };
      return { ...next, marks: p.marks };
    }
    case "spotlight":
      return { ...next, spotlight: { targets: p.targets ?? [], findingId: p.finding_id ?? null, reason: p.reason ?? "", seq: event.sequence } };
    case "investigation_updated": {
      const prev = next.investigations[p.id] ?? { id: p.id, suspects: [] };
      const inv = { ...prev, ...p, suspects: p.suspects ?? prev.suspects };
      const order = next.investigationOrder.includes(p.id) ? next.investigationOrder : [...next.investigationOrder, p.id];
      next = { ...next, investigations: { ...next.investigations, [p.id]: inv }, investigationOrder: order };
      return upsertTrail(next, { id: `inv-${p.id}`, kind: "investigation", investigationId: p.id, text: inv.question, status: inv.status, at, seq: event.sequence });
    }
    case "suspect_updated": {
      const inv = next.investigations[p.investigation_id];
      if (!inv) return next;
      const suspects = inv.suspects.map((s) => (s.id === p.suspect_id ? { ...s, status: p.status, conclusion: p.conclusion ?? s.conclusion } : s));
      next = { ...next, investigations: { ...next.investigations, [inv.id]: { ...inv, suspects } } };
      return upsertTrail(next, {
        id: `sus-${p.suspect_id}-${event.sequence}`, kind: "suspect", status: p.status,
        text: suspects.find((s) => s.id === p.suspect_id)?.title ?? p.suspect_id, detail: p.conclusion, at, seq: event.sequence,
      });
    }
    case "sim_queued": {
      const sim = { id: p.job_id, label: p.label, kind: p.kind, quality: p.quality, change: p.change ?? null, status: "queued", pct: 0, step: "queued" };
      next = { ...next, sims: { ...next.sims, [p.job_id]: sim } };
      return upsertTrail(next, { id: `sim-${p.job_id}`, kind: "sim", simId: p.job_id, text: p.label, state: "queued", pct: 0, at, seq: event.sequence });
    }
    case "sim_progress": {
      const sim = next.sims[p.job_id];
      if (!sim) return next;
      next = { ...next, sims: { ...next.sims, [p.job_id]: { ...sim, status: "running", pct: p.pct, step: p.step ?? sim.step } } };
      return upsertTrail(next, { id: `sim-${p.job_id}`, kind: "sim", simId: p.job_id, text: sim.label, state: "running", pct: p.pct, at, seq: event.sequence });
    }
    case "sim_done": {
      const sim = next.sims[p.job_id] ?? { id: p.job_id, label: p.label ?? p.job_id };
      const done = { ...sim, status: "done", pct: 1, summary: p.summary, curve: p.curve ?? sim.curve, trust: p.trust ?? "ok" };
      next = { ...next, sims: { ...next.sims, [p.job_id]: done } };
      const exp = { id: p.job_id, label: done.label, kind: done.kind, change: done.change, summary: p.summary, curve: done.curve, trust: done.trust };
      next = { ...next, experiments: [...next.experiments.filter((e) => e.id !== exp.id), exp] };
      return upsertTrail(next, { id: `sim-${p.job_id}`, kind: "sim", simId: p.job_id, text: done.label, state: "done", pct: 1, summary: p.summary, at, seq: event.sequence });
    }
    case "report_ready":
      return upsertTrail({ ...next, reportReady: { revision: p.revision ?? 1 } }, { id: `report-${event.sequence}`, kind: "result", text: `Report r${p.revision ?? 1} is ready`, at, seq: event.sequence });
    case "completed":
      return { ...next, status: "complete", endState: p.end_state ?? "complete" };
    case "failed":
      return { ...next, status: "failed", endState: p.end_state ?? "pipeline_failed" };
    case "canceled":
      return { ...next, status: "canceled", endState: "canceled" };

    /* ------------- follow-up (section 17) ------------- */
    case "user_message": {
      const f = next.followup;
      return { ...next, followup: { ...f, running: true, messages: [...f.messages, { id: `m-${event.sequence}`, role: "user", text: p.text, selected: p.selected ?? [] }] } };
    }
    case "followup_started":
      return { ...next, followup: { ...next.followup, level: p.level, selected: p.selected ?? [], running: true } };
    case "text_delta": {
      const f = next.followup;
      const msgs = f.messages.slice();
      const last = msgs[msgs.length - 1];
      if (last && last.role === "agent" && last.turn === event.turn_id) msgs[msgs.length - 1] = { ...last, text: last.text + p.text };
      else msgs.push({ id: `m-${event.sequence}`, role: "agent", turn: event.turn_id, text: p.text });
      return { ...next, followup: { ...f, messages: msgs } };
    }
    case "test_proposed":
      return { ...next, followup: { ...next.followup, proposal: { request_id: p.request_id, what: p.what, why: p.why, eta_s: p.eta_s, status: "pending" } } };
    case "test_decided": {
      const pr = next.followup.proposal;
      if (!pr || pr.request_id !== p.request_id) return next;
      return { ...next, followup: { ...next.followup, proposal: { ...pr, status: p.approved ? "approved" : "declined" } } };
    }
    case "whatif_ready":
      return { ...next, followup: { ...next.followup, whatif: p } };
    case "finding_changed": {
      const f = p.finding;
      next = addFinding(next, f);
      return upsertTrail(next, { id: `find-${f.id}-${event.sequence}`, kind: "finding", findingId: f.id, text: `${f.id} ${p.change ?? "changed"}`, severity: f.severity, at, seq: event.sequence });
    }
    case "assumption_changed": {
      const findings = { ...next.findings };
      for (const id of p.affects ?? []) if (findings[id]) findings[id] = { ...findings[id], status: "needs_recheck" };
      return { ...next, findings, assumptions: [...next.assumptions.filter((a) => a.id !== p.assumption?.id), p.assumption].filter(Boolean) };
    }
    case "report_revised":
      return { ...next, reportReady: { revision: p.revision }, notices: [...next.notices, { id: `rev-${p.revision}`, text: `Report updated to r${p.revision}` }] };
    case "followup_done":
      return { ...next, followup: { ...next.followup, running: false } };
    default:
      return next;
  }
}

export function replayAll(events, state = initialReviewState()) {
  return events.reduce(reviewReducer, state);
}

export const SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"];

export function severityCounts(findings) {
  const out = { critical: 0, high: 0, medium: 0, low: 0, info: 0 };
  for (const f of findings) if (f.status !== "dismissed" && f.status !== "superseded") out[f.severity]++;
  return out;
}

export function sortFindings(findings) {
  return [...findings].sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity) || a.id.localeCompare(b.id));
}

/** All marks that should draw on the board: finding marks plus loose marks. */
export function activeMarks(state) {
  const out = [];
  for (const id of state.findingOrder) {
    const f = state.findings[id];
    if (!f || f.status === "dismissed" || f.status === "superseded") continue;
    f.marks.forEach((m, i) => out.push({ key: `${id}:${i}`, findingId: id, severity: f.severity, mark: m }));
  }
  state.marks.forEach((m, i) => out.push({ key: `loose:${i}`, findingId: null, severity: "info", mark: m }));
  return out;
}

/** Findings with a pin number (stable order of arrival). */
export function numberFindings(state) {
  const map = new Map();
  state.findingOrder.forEach((id, i) => map.set(id, i + 1));
  return map;
}
