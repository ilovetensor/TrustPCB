// Client reducer over review and thread events.
// Pure and deterministic so a live stream, a replay and a page reload agree.
// Payload shapes follow what the backend emits (server/trustpcb/review/*.py).

export const PHASES = [
  { id: 0, key: "intake", label: "Intake", short: "INTAKE", actor: "code" },
  { id: 1, key: "baseline", label: "Baseline", short: "BASELINE", actor: "code" },
  { id: 2, key: "understand", label: "Understand", short: "UNDERSTAND", actor: "agent" },
  { id: 3, key: "review", label: "Review", short: "REVIEW", actor: "agent" },
  { id: 4, key: "investigate", label: "Investigate", short: "INVESTIGATE", actor: "agent" },
  { id: 5, key: "confirm", label: "Confirm", short: "CONFIRM", actor: "agent" },
  { id: 6, key: "report", label: "Report", short: "REPORT", actor: "code" },
];

/** mode "review" reads the review chat. mode "thread" reads one follow-up conversation. */
export function initialReviewState(mode = "review") {
  return {
    mode,
    status: "idle", // idle | running | complete | failed | canceled
    endState: null,
    failure: null,
    phase: -1,
    seen: -1,
    budget: null,
    /** when the budget numbers were true, from the event's own timestamp (ms), so a clock can count on from there */
    budgetAt: null,
    trail: [],
    findings: {},
    findingOrder: [],
    marks: [], // loose marks not tied to a finding (marks_changed with finding_id null)
    brief: null,
    /** the bounded second attempt after a confirm that missed the target: phase stays 5, substep refine */
    refine: null,
    /** analyzer results the agent judged harmless: [{rule, count, reason}] */
    cleared: [],
    /** phases the server skipped, with its reason: [{phase, name, reason}] */
    skipped: [],
    candidates: { total: 0, reviewed: 0, byRule: {} },
    investigations: {},
    investigationOrder: [],
    sims: {},
    experiments: [],
    spotlight: null,
    reportReady: null,
    assumptions: [],
    requests: {}, // interactions awaiting a decision, by request_id
    followup: { level: null, selected: [], messages: [], proposal: null, whatif: null, running: false, endedTurn: null },
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
  const prev = state.findings[finding.id] ?? {};
  return {
    ...state,
    findings: { ...state.findings, [finding.id]: { marks: [], ...prev, ...finding } },
    findingOrder: exists ? state.findingOrder : [...state.findingOrder, finding.id],
  };
}

function findingByCode(state, code) {
  if (!code) return null;
  return state.findingOrder.map((id) => state.findings[id]).find((f) => f && f.code === code) ?? null;
}

const TOOL_LABELS = {
  get_board_summary: "Read board summary",
  view_board: "Looked at the board",
  read_schematic: "Read schematic",
  read_datasheet: "Read a datasheet",
  list_candidates: "Listed analyzer candidates",
  dismiss_candidate: "Dismissed a candidate",
  record_finding: "Recorded a finding",
  show_on_board: "Pointed at the board",
  open_investigation: "Opened an investigation",
  close_investigation: "Closed an investigation",
  run_simulation: "Ran a simulation",
  view_result: "Viewed a result",
  compare_results: "Compared results",
  tune_matching: "Calculated matching",
  conclude_suspect: "Concluded a suspect",
  confirm_result: "Confirmation run",
  submit_board_brief: "Submitted the board brief",
  submit_summary: "Submitted the summary",
  advance_phase: "Moved to the next phase",
  get_status: "Checked status",
  run_analyzer: "Ran an analyzer",
  measure: "Measured",
  try_placement: "Tried a placement",
};

export function toolLabel(tool) {
  const t = tool ?? "tool";
  return TOOL_LABELS[t] ?? t.replace(/_/g, " ");
}

/** "Refine round 2: ..." is a suspect added by a refine round. */
export const roundOf = (title) => { const m = /^Refine round (\d+)/i.exec(title ?? ""); return m ? Number(m[1]) : undefined; };

const TERMINAL_STATUS = { completed: "complete", failed: "failed", canceled: "canceled" };

/** Readable text of a tool_result's content, for the trail detail. */
export function resultText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.filter((c) => c && c.type === "text" && typeof c.text === "string").map((c) => c.text).join("\n");
}

export function reviewReducer(state, event) {
  if (event.sequence <= state.seen) return state;
  const p = event.payload ?? {};
  let next = { ...state, seen: event.sequence };
  const at = event.created_at;
  const thread = state.mode === "thread";
  switch (event.type) {
    case "status": {
      if (p.state === "max_turns") return { ...next, notices: [...next.notices, { id: `st-${event.sequence}`, text: "The agent reached its turn limit." }] };
      if (p.state === "session_rebuilt" && p.message) return { ...next, notices: [...next.notices, { id: `st-${event.sequence}`, text: p.message }] };
      if (p.state === "baseline" && p.step) {
        return upsertTrail({ ...next, status: next.status === "idle" ? "running" : next.status }, {
          id: `base-${p.step}`, kind: "result", text: `Baseline: ${String(p.step).replace(/_/g, " ")}`, detail: p.status && p.status !== "ok" ? String(p.status) : undefined, at, seq: event.sequence,
        });
      }
      return next;
    }
    case "phase_changed": {
      // A refine round keeps the phase at 5 (events stay forward-only) and says so in `substep`.
      const refine = p.substep === "refine" ? { round: p.round ?? 1, rounds: p.rounds ?? null, investigation: p.investigation ?? null } : null;
      next = { ...next, status: "running", phase: p.phase, refine };
      const label = refine ? `Refine round ${refine.round}${refine.rounds ? ` of ${refine.rounds}` : ""}` : p.label ?? PHASES[p.phase]?.label ?? p.name ?? "";
      next = upsertTrail(next, { id: `phase-${p.phase}-${event.sequence}`, kind: "phase", phase: p.phase, substep: refine ? "refine" : undefined, round: refine?.round, text: label, at, seq: event.sequence });
      for (const s of p.skipped ?? []) {
        const reason = typeof s.reason === "string" && s.reason ? s.reason.charAt(0).toUpperCase() + s.reason.slice(1) : s.reason;
        next = upsertTrail(next, { id: `skip-${s.phase}-${event.sequence}`, kind: "result", text: `Skipped ${s.name ?? PHASES[s.phase]?.label}`, detail: reason, at, seq: event.sequence });
        next = { ...next, skipped: [...next.skipped.filter((x) => x.phase !== s.phase), { phase: s.phase, name: s.name ?? PHASES[s.phase]?.label ?? "", reason: reason ?? "" }] };
      }
      return next;
    }
    case "budget":
      return { ...next, budget: { ...(next.budget ?? {}), ...p }, budgetAt: Number.isFinite(Date.parse(at)) ? Date.parse(at) : next.budgetAt };
    case "thought": {
      if (thread) {
        // The thread's reply normally arrives as text_delta. A reply that only exists as a thought still has to be readable.
        const f = next.followup;
        if (p.text && !f.messages.some((m) => m.role === "agent" && m.turn === event.turn_id))
          return { ...next, followup: { ...f, messages: [...f.messages, { id: `m-${event.sequence}`, role: "agent", turn: event.turn_id, text: p.text, closed: true }] } };
        return next;
      }
      return upsertTrail(next, { id: `th-${event.sequence}`, kind: "thought", text: p.text, phase: p.phase ?? next.phase, at, seq: event.sequence });
    }
    case "tool_start": {
      const id = `tool-${p.tool_call_id ?? event.sequence}`;
      if (thread) {
        // Text before a working tool is narration. The reply is what is written after the last one, or what end_followup streams.
        const msgs = next.followup.messages.slice();
        const last = msgs[msgs.length - 1];
        if (last && last.role === "agent" && !last.closed && last.turn === event.turn_id) {
          msgs[msgs.length - 1] = { ...last, closed: true, narration: p.name !== "end_followup" };
          next = { ...next, followup: { ...next.followup, messages: msgs } };
        }
      }
      return upsertTrail(next, { id, kind: "tool", tool: p.name, input: p.input, text: p.trail ?? toolLabel(p.name), state: "running", phase: next.phase, at, seq: event.sequence });
    }
    case "tool_result": {
      const id = `tool-${p.tool_call_id ?? event.sequence}`;
      const prev = next.trail.find((e) => e.id === id);
      const name = p.name ?? prev?.tool;
      if ((name === "dismiss_rule_group" || name === "dismiss_candidate") && !p.is_error) {
        let data = null;
        try { data = JSON.parse(resultText(p.content)); } catch { /* not JSON */ }
        const input = prev?.input ?? {};
        const count = typeof data?.count === "number" ? data.count : Array.isArray(input.candidate_ids) ? input.candidate_ids.length : null;
        const reason = data?.reason ?? input.reason ?? "";
        if (count) next = { ...next, cleared: [...next.cleared.filter((c) => c.id !== id), { id, rule: data?.dismissed_rule ?? input.rule ?? null, count, reason }] };
      }
      // end_followup closes the reply. Text a model writes after it is the same answer again, so it is not shown.
      if (thread && name === "end_followup" && !p.is_error) next = { ...next, followup: { ...next.followup, endedTurn: event.turn_id } };
      return upsertTrail(next, {
        id, kind: "tool", tool: p.name ?? prev?.tool, text: prev?.text ?? toolLabel(p.name), state: p.is_error ? "error" : "done",
        result: resultText(p.content).slice(0, 600), phase: next.phase, at: prev?.at ?? at, seq: prev?.seq ?? event.sequence,
      });
    }
    case "candidate_batch": {
      const c = next.candidates;
      const total = p.total ?? c.total;
      const fresh = p.counts && typeof p.counts.new === "number" ? p.counts.new : null;
      return { ...next, candidates: { total, reviewed: fresh === null ? c.reviewed : Math.max(0, total - fresh), byRule: { ...c.byRule, ...(p.counts ?? {}) } } };
    }
    case "brief_ready": {
      const brief = { ...(p.brief ?? {}), zones: p.zones ?? p.brief?.roles ?? {} };
      return upsertTrail({ ...next, brief }, { id: `brief-${event.sequence}`, kind: "result", text: "Board brief ready", detail: brief.purpose, at, seq: event.sequence });
    }
    case "finding_upserted": {
      const f = p.finding;
      if (!f) return next;
      next = addFinding(next, f);
      return upsertTrail(next, { id: `find-${f.id}`, kind: "finding", findingId: f.id, text: `${f.code ?? ""} ${f.title}`.trim(), severity: f.severity, at, seq: event.sequence });
    }
    case "marks_changed": {
      const target = p.finding_id && next.findings[p.finding_id] ? p.finding_id : findingByCode(next, p.code)?.id;
      if (target) return { ...next, findings: { ...next.findings, [target]: { ...next.findings[target], marks: p.marks ?? [] } } };
      if (p.finding_id) {
        // marks arrived before the card (should not happen, the card comes first): hold them on the finding id
        return addFinding(next, { id: p.finding_id, code: p.code, title: p.code ?? "Finding", severity: "info", class: "opinion", status: "open", what: "", why_it_matters: "", recommendation: "", marks: p.marks ?? [], assumptions: [], not_tested: [], evidence: [] });
      }
      return { ...next, marks: p.marks ?? [] };
    }
    case "spotlight": {
      const f = findingByCode(next, p.finding_code);
      return { ...next, spotlight: { targets: p.targets ?? [], findingId: f?.id ?? null, reason: p.label ?? "", seq: event.sequence } };
    }
    case "investigation_updated": {
      const id = p.investigation_id;
      if (!id) return next;
      const prev = next.investigations[id] ?? { id, suspects: [] };
      const suspects = (p.suspects ?? []).map((s) => {
        const old = prev.suspects.find((x) => x.code === s.code);
        return { ...(old ?? {}), id: old?.id ?? s.code, code: s.code, title: s.title, status: s.status, conclusion: old?.conclusion, round: roundOf(s.title) ?? old?.round };
      });
      const inv = { ...prev, id, code: p.code, question: p.question ?? prev.question, status: p.status, outcome: p.outcome ?? prev.outcome, goodEnough: p.good_enough ?? prev.goodEnough, suspects: suspects.length ? suspects : prev.suspects };
      const order = next.investigationOrder.includes(id) ? next.investigationOrder : [...next.investigationOrder, id];
      next = { ...next, investigations: { ...next.investigations, [id]: inv }, investigationOrder: order };
      return upsertTrail(next, { id: `inv-${id}`, kind: "investigation", investigationId: id, text: inv.question, status: inv.status, at, seq: event.sequence });
    }
    case "suspect_updated": {
      const inv = next.investigations[p.investigation_id];
      if (!inv) return next;
      let found = false;
      const suspects = inv.suspects.map((s) => {
        if (s.code !== p.code && s.id !== p.suspect_id) return s;
        found = true;
        return { ...s, status: p.status, title: p.title ?? s.title, conclusion: p.reason ?? s.conclusion, round: roundOf(p.title ?? s.title) ?? s.round };
      });
      if (!found) suspects.push({ id: p.suspect_id, code: p.code, title: p.title, status: p.status, conclusion: p.reason, round: roundOf(p.title) });
      next = { ...next, investigations: { ...next.investigations, [inv.id]: { ...inv, suspects } } };
      return upsertTrail(next, { id: `sus-${p.suspect_id}-${event.sequence}`, kind: "suspect", status: p.status, text: p.title ?? p.code, detail: p.reason, at, seq: event.sequence });
    }
    case "sim_queued": {
      const sim = { id: p.ref, label: p.label, kind: p.kind, quality: p.quality, suspect: p.suspect, hash: p.hash, change: null, status: "queued", pct: 0, step: "queued" };
      next = { ...next, sims: { ...next.sims, [p.ref]: sim } };
      return upsertTrail(next, { id: `sim-${p.ref}`, kind: "sim", simId: p.ref, text: p.label, state: "queued", pct: 0, at, seq: event.sequence });
    }
    case "sim_progress": {
      const sim = next.sims[p.ref];
      if (!sim) return next;
      const pct = typeof p.fraction === "number" ? p.fraction : sim.pct;
      next = { ...next, sims: { ...next.sims, [p.ref]: { ...sim, status: "running", pct, step: p.phase ?? p.step ?? sim.step } } };
      return upsertTrail(next, { id: `sim-${p.ref}`, kind: "sim", simId: p.ref, text: sim.label, state: "running", pct, at, seq: event.sequence });
    }
    case "sim_done": {
      const sim = next.sims[p.ref] ?? { id: p.ref, label: p.label ?? p.ref, kind: p.kind };
      const trust = p.trust?.status ?? (p.ok ? "ok" : "failed");
      const done = { ...sim, suspect: p.suspect ?? sim.suspect, hash: p.hash ?? sim.hash, quality: p.quality ?? sim.quality, status: "done", pct: 1, measurements: p.measurements ?? {}, delta: p.delta ?? null, curve: p.curve ?? null, trust, ok: !!p.ok, meetsTarget: p.meets_target, evidence: p.evidence, solveS: p.solve_s };
      next = { ...next, sims: { ...next.sims, [p.ref]: done } };
      const exp = { id: p.ref, label: done.label, kind: done.kind, quality: p.quality, suspect: done.suspect, hash: done.hash, solveS: p.solve_s, measurements: done.measurements, delta: done.delta, curve: done.curve, trust, ok: done.ok, meetsTarget: p.meets_target, evidence: p.evidence };
      next = { ...next, experiments: [...next.experiments.filter((e) => e.id !== exp.id), exp] };
      return upsertTrail(next, { id: `sim-${p.ref}`, kind: "sim", simId: p.ref, text: done.label, state: "done", pct: 1, summary: done.measurements, at, seq: event.sequence });
    }
    case "report_ready":
      return upsertTrail({ ...next, reportReady: { revision: p.revision ?? 1 } }, { id: `report-${event.sequence}`, kind: "result", text: `Report r${p.revision ?? 1} is ready`, at, seq: event.sequence });
    case "completed":
    case "failed":
    case "canceled": {
      const fu = { ...next.followup, running: false };
      if (thread) {
        const msg = event.type === "failed" ? [{ id: `err-${event.sequence}`, role: "agent", error: true, turn: event.turn_id, text: p.message || "The reply failed." }] : [];
        return { ...next, followup: { ...fu, messages: [...fu.messages, ...msg] } };
      }
      return {
        ...next, followup: fu, status: TERMINAL_STATUS[event.type],
        endState: event.type === "canceled" ? "canceled" : p.interrupted ? "interrupted" : p.end_state ?? (event.type === "failed" ? "pipeline_failed" : "complete"),
        failure: event.type === "failed" ? p.message ?? null : null,
      };
    }

    /* ------------- interactions: approval cards ------------- */
    case "test":
    case "permission":
    case "question": {
      if (!p.request_id) return next;
      const req = { request_id: p.request_id, kind: event.type, payload: p, status: "pending" };
      next = { ...next, requests: { ...next.requests, [p.request_id]: req } };
      if (event.type === "test") {
        return { ...next, followup: { ...next.followup, proposal: { request_id: p.request_id, what: p.title, why: p.why, eta_s: p.estimated_seconds, plan: p.plan, limits: p.limits, status: "pending" } } };
      }
      return next;
    }
    case "request_resolved": {
      const r = next.requests[p.request_id];
      const status = p.allow === false ? "declined" : "approved";
      if (r) next = { ...next, requests: { ...next.requests, [p.request_id]: { ...r, status } } };
      const pr = next.followup.proposal;
      if (pr && pr.request_id === p.request_id) next = { ...next, followup: { ...next.followup, proposal: { ...pr, status } } };
      return next;
    }

    /* ------------- follow-up (section 17) ------------- */
    case "user_message": {
      if (!thread) return next;
      const f = next.followup;
      return { ...next, followup: { ...f, running: true, messages: [...f.messages, { id: `m-${event.sequence}`, role: "user", text: p.text, selected: (p.selected ?? []).map((s) => (typeof s === "string" ? s : s.id ?? s.target ?? s.code)) }] } };
    }
    case "followup_started":
      return { ...next, followup: { ...next.followup, level: p.level, selected: p.selected ?? [], running: true } };
    case "text_delta": {
      if (!thread || !p.text) return next;
      const f = next.followup;
      if (f.endedTurn && f.endedTurn === event.turn_id) return next;
      const msgs = f.messages.slice();
      const last = msgs[msgs.length - 1];
      if (last && last.role === "agent" && !last.error && !last.closed && last.turn === event.turn_id) msgs[msgs.length - 1] = { ...last, text: last.text + p.text };
      else msgs.push({ id: `m-${event.sequence}`, role: "agent", turn: event.turn_id, text: p.text });
      return { ...next, followup: { ...f, messages: msgs } };
    }
    case "test_proposed": {
      if (next.followup.proposal) return next; // the `test` interaction event carries the request id and wins
      return { ...next, followup: { ...next.followup, proposal: { request_id: null, what: p.title, why: p.why, eta_s: p.estimated_seconds, plan: p.plan, limits: p.limits, status: "pending" } } };
    }
    case "test_decided": {
      const pr = next.followup.proposal;
      if (!pr) return next;
      return { ...next, followup: { ...next.followup, proposal: { ...pr, status: p.allow === false ? "declined" : "approved" } } };
    }
    case "whatif_ready":
      return { ...next, followup: { ...next.followup, whatif: p } };
    case "finding_changed": {
      const f = p.finding;
      if (f) {
        next = addFinding(next, f);
      } else {
        const cur = next.findings[p.finding_id] ?? findingByCode(next, p.code);
        if (!cur) return next;
        next = { ...next, findings: { ...next.findings, [cur.id]: { ...cur, status: p.status ?? cur.status } } };
      }
      const id = f?.id ?? p.finding_id;
      return upsertTrail(next, { id: `find-${id}-${event.sequence}`, kind: "finding", findingId: id, text: `${p.code ?? f?.code ?? ""} ${p.status ?? "changed"}`.trim(), detail: p.status ?? "changed", severity: (f ?? next.findings[id])?.severity, at, seq: event.sequence });
    }
    case "assumption_changed": {
      const findings = { ...next.findings };
      for (const a of p.affected ?? []) {
        const f = findingByCode(next, a.code);
        if (f) findings[f.id] = { ...findings[f.id], status: a.status ?? "needs_recheck" };
      }
      const assumption = p.assumption ? { id: p.assumption.id, text: `${String(p.assumption.name).replace(/_/g, " ")}: ${typeof p.assumption.value === "object" ? JSON.stringify(p.assumption.value) : p.assumption.value}`, source: p.assumption.source } : null;
      return { ...next, findings, assumptions: assumption ? [...next.assumptions.filter((a) => a.id !== assumption.id), assumption] : next.assumptions };
    }
    case "report_revised":
      return { ...next, reportReady: { revision: p.revision }, notices: [...next.notices, { id: `rev-${p.revision}`, text: `Report updated to r${p.revision}` }] };
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
  return [...findings].sort((a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity) || (a.code ?? a.id).localeCompare(b.code ?? b.id));
}

/** All marks that should draw on the board: finding marks plus loose marks. */
export function activeMarks(state) {
  const out = [];
  for (const id of state.findingOrder) {
    const f = state.findings[id];
    if (!f || f.status === "dismissed" || f.status === "superseded") continue;
    (f.marks ?? []).forEach((m, i) => out.push({ key: `${id}:${i}`, findingId: id, severity: f.severity, mark: m }));
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
