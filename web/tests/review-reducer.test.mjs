import { test } from "node:test";
import assert from "node:assert/strict";
import { initialReviewState, reviewReducer, replayAll, activeMarks, severityCounts, sortFindings, PHASES } from "../lib/review-reducer.mjs";

const ev = (sequence, type, payload) => ({ sequence, type, payload, turn_id: "t", created_at: "2026-10-31T00:00:00Z" });
const F = (id, severity, marks = []) => ({ id, title: id, severity, class: "measured", status: "open", marks, evidence_ids: [], assumptions: [], not_tested: [] });

test("phase and budget events move the tracker", () => {
  const s = replayAll([ev(1, "phase_changed", { phase: 3, label: "Review" }), ev(2, "budget", { wall_left_s: 100, wall_total_s: 200 })]);
  assert.equal(s.phase, 3);
  assert.equal(PHASES[s.phase].key, "review");
  assert.equal(s.status, "running");
  assert.equal(s.budget.wall_left_s, 100);
});

test("duplicate or out-of-order sequences are ignored", () => {
  let s = reviewReducer(initialReviewState(), ev(5, "thought", { text: "a" }));
  s = reviewReducer(s, ev(5, "thought", { text: "dup" }));
  s = reviewReducer(s, ev(3, "thought", { text: "old" }));
  assert.equal(s.trail.length, 1);
});

test("finding_upserted adds card and marks together; upsert updates in place", () => {
  const m = { type: "pin", targets: ["ref:C12"] };
  let s = replayAll([ev(1, "finding_upserted", { finding: F("F-001", "high", [m]) })]);
  assert.equal(s.findingOrder.length, 1);
  assert.equal(activeMarks(s).length, 1);
  s = reviewReducer(s, ev(2, "finding_upserted", { finding: F("F-001", "critical", [m, m]) }));
  assert.equal(s.findingOrder.length, 1);
  assert.equal(s.findings["F-001"].severity, "critical");
  assert.equal(activeMarks(s).length, 2);
});

test("tool_start then tool_result updates one trail entry", () => {
  const s = replayAll([ev(1, "tool_start", { call_id: "c1", tool: "measure" }), ev(2, "tool_result", { call_id: "c1", tool: "measure", ok: true, result: "7.66 mm" })]);
  assert.equal(s.trail.length, 1);
  assert.equal(s.trail[0].state, "done");
});

test("simulation lifecycle fills the experiment strip", () => {
  const s = replayAll([
    ev(1, "sim_queued", { job_id: "a", label: "Base", kind: "base" }),
    ev(2, "sim_progress", { job_id: "a", pct: 0.5 }),
    ev(3, "sim_done", { job_id: "a", summary: { f0_ghz: 2.3, s11_db: -12, bw_mhz: 50 }, curve: "base" }),
  ]);
  assert.equal(s.sims.a.status, "done");
  assert.equal(s.experiments.length, 1);
  assert.equal(s.trail.length, 1);
});

test("suspect updates require the investigation and change status", () => {
  const s = replayAll([
    ev(1, "investigation_updated", { id: "i", question: "Q?", status: "running", suspects: [{ id: "s1", title: "A", status: "pending" }] }),
    ev(2, "suspect_updated", { investigation_id: "i", suspect_id: "s1", status: "ruled_out", conclusion: "no effect" }),
    ev(3, "suspect_updated", { investigation_id: "nope", suspect_id: "s1", status: "confirmed" }),
  ]);
  assert.equal(s.investigations.i.suspects[0].status, "ruled_out");
});

test("assumption_changed marks dependents as needing recheck, dismissed findings leave counts", () => {
  let s = replayAll([ev(1, "finding_upserted", { finding: F("F-002", "medium") }), ev(2, "finding_upserted", { finding: F("F-003", "low") })]);
  s = reviewReducer(s, ev(3, "assumption_changed", { assumption: { id: "A-1", text: "x" }, affects: ["F-002"] }));
  assert.equal(s.findings["F-002"].status, "needs_recheck");
  const counts = severityCounts([{ ...s.findings["F-002"], status: "dismissed" }, s.findings["F-003"]]);
  assert.equal(counts.medium, 0);
  assert.equal(counts.low, 1);
  assert.deepEqual(sortFindings(Object.values(s.findings)).map((f) => f.id), ["F-002", "F-003"]);
});

test("follow-up text deltas stream into one agent message and proposals resolve", () => {
  let s = replayAll([
    ev(1, "user_message", { text: "hi", selected: [] }),
    ev(2, "text_delta", { text: "Hel" }),
    ev(3, "text_delta", { text: "lo" }),
    ev(4, "test_proposed", { request_id: "r1", what: "w", why: "y", eta_s: 60 }),
  ]);
  assert.equal(s.followup.messages.at(-1).text, "Hello");
  assert.equal(s.followup.proposal.status, "pending");
  s = reviewReducer(s, ev(5, "test_decided", { request_id: "r1", approved: false }));
  assert.equal(s.followup.proposal.status, "declined");
});
