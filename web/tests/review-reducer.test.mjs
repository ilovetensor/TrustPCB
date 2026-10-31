import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { initialReviewState, reviewReducer, replayAll, activeMarks, severityCounts, sortFindings, PHASES } from "../lib/review-reducer.mjs";

const ev = (sequence, type, payload, turn_id = "t") => ({ sequence, type, payload, turn_id, created_at: "2026-10-31T00:00:00Z" });
const F = (id, severity, marks = []) => ({ id, code: id, title: id, severity, class: "measured", status: "open", marks, evidence: [], assumptions: [], not_tested: [] });

// A real event log captured from a live quick review (no model was involved in this test).
const real = JSON.parse(fs.readFileSync(new URL("./data/quick_review_events.json", import.meta.url)));

test("a real quick review replays to a complete state with its findings and marks", () => {
  const s = replayAll(real);
  assert.equal(s.status, "complete");
  assert.equal(s.endState, "complete");
  assert.equal(s.phase, 6);
  assert.equal(s.findingOrder.length, 2);
  const [a, b] = s.findingOrder.map((id) => s.findings[id]);
  assert.equal(a.code, "F-001");
  assert.equal(b.code, "F-002");
  assert.ok(a.marks.length > 0 && a.marks[0].resolved?.length, "marks carry server-resolved geometry");
  assert.equal(activeMarks(s).length, a.marks.length + b.marks.length);
  assert.deepEqual(s.reportReady, { revision: 1 });
  assert.equal(s.budget.wall_s.limit, 240);
  assert.equal(s.budget.turns.used, 10);
  assert.equal(s.candidates.total, 358);
  assert.equal(s.candidates.reviewed, 7, "reviewed is the total minus the ones still new");
});

test("tool_start carries the readable trail text and tool_result closes the same entry", () => {
  const s = replayAll(real);
  const read = s.trail.find((e) => e.kind === "tool" && e.tool === "get_board_summary");
  assert.equal(read.text, "Reading the board summary");
  assert.equal(read.state, "done");
  assert.ok(s.trail.filter((e) => e.kind === "tool" && e.tool === "get_board_summary").length === 1);
  const skipped = s.trail.filter((e) => e.kind === "result" && /^Skipped/.test(e.text));
  assert.equal(skipped.length, 2, "phases 4 and 5 are shown as skipped for a quick review");
});

test("brief_ready keeps the brief and its zones", () => {
  const s = replayAll(real);
  assert.match(s.brief.purpose, /nRF52840/);
  assert.ok(s.brief.zones.radio.includes("ref:U3"));
});

test("replaying from any prefix and then the rest equals one full replay", () => {
  const full = replayAll(real);
  const mid = replayAll(real.slice(0, 20));
  const resumed = replayAll(real.slice(20), mid);
  assert.equal(resumed.findingOrder.length, full.findingOrder.length);
  assert.equal(resumed.status, full.status);
  assert.equal(resumed.trail.length, full.trail.length);
});

test("phase and budget events move the tracker", () => {
  const s = replayAll([ev(1, "phase_changed", { phase: 3, name: "review", previous: 2 }), ev(2, "budget", { wall_s: { used: 10, limit: 200, left: 190 } })]);
  assert.equal(s.phase, 3);
  assert.equal(PHASES[s.phase].key, "review");
  assert.equal(s.status, "running");
  assert.equal(s.budget.wall_s.left, 190);
});

test("a budget event keeps the time it was true, so a clock can count on from it after a reload", () => {
  const s = replayAll([ev(1, "budget", { wall_s: { used: 10, limit: 200, left: 190 } })]);
  assert.equal(s.budgetAt, Date.parse("2026-10-31T00:00:00Z"));
  assert.equal(initialReviewState().budgetAt, null);
});

test("duplicate or out-of-order sequences are ignored", () => {
  let s = reviewReducer(initialReviewState(), ev(5, "thought", { text: "a" }));
  s = reviewReducer(s, ev(5, "thought", { text: "dup" }));
  s = reviewReducer(s, ev(3, "thought", { text: "old" }));
  assert.equal(s.trail.length, 1);
});

test("finding_upserted adds card and marks together; marks_changed finds the card by id or code", () => {
  const m = { type: "pin", targets: ["ref:C12"] };
  let s = replayAll([ev(1, "finding_upserted", { finding: F("a1", "high", [m]) })]);
  assert.equal(s.findingOrder.length, 1);
  assert.equal(activeMarks(s).length, 1);
  s = reviewReducer(s, ev(2, "finding_upserted", { finding: F("a1", "critical", [m, m]) }));
  assert.equal(s.findingOrder.length, 1);
  assert.equal(s.findings.a1.severity, "critical");
  assert.equal(activeMarks(s).length, 2);
  s = reviewReducer(s, ev(3, "marks_changed", { finding_id: "zzz", code: "a1", marks: [m] }));
  assert.equal(s.findings.a1.marks.length, 1, "falls back to the display code");
});

test("spotlight resolves its finding by code", () => {
  let s = replayAll([ev(1, "finding_upserted", { finding: { ...F("hex1", "high"), code: "F-007" } })]);
  s = reviewReducer(s, ev(2, "spotlight", { targets: ["ref:U3"], finding_code: "F-007", label: "look", resolved: [] }));
  assert.equal(s.spotlight.findingId, "hex1");
});

test("a simulation waiting for the shared solver stays queued, says its place, and runs when it gets the solver", () => {
  let s = replayAll([
    ev(1, "sim_queued", { ref: "S-9", hash: "h", label: "Power, widen 3V3", kind: "power", quality: "screen" }),
    ev(2, "sim_progress", { ref: "S-9", hash: "h", phase: "waiting", position: 2, ahead: 2 }),
  ]);
  assert.equal(s.sims["S-9"].status, "queued");
  assert.deepEqual(s.sims["S-9"].waiting, { position: 2, ahead: 2 });
  assert.equal(s.sims["S-9"].kind, "power");
  assert.equal(s.trail[0].state, "queued");
  s = reviewReducer(s, ev(3, "sim_progress", { ref: "S-9", hash: "h", phase: "started" }));
  assert.equal(s.sims["S-9"].status, "running");
  assert.equal(s.sims["S-9"].waiting, null);
  s = reviewReducer(s, ev(4, "sim_progress", { ref: "S-9", hash: "h", fraction: 0.4, phase: "solving" }));
  assert.equal(s.sims["S-9"].pct, 0.4);
  assert.equal(s.trail[0].state, "running");
});

test("simulation lifecycle fills the experiment strip from the backend's field names", () => {
  const s = replayAll([
    ev(1, "sim_queued", { ref: "S-1", hash: "h", label: "Base", kind: "base", quality: "screen", suspect: "X-1" }),
    ev(2, "sim_progress", { ref: "S-1", hash: "h", fraction: 0.5, phase: "solving" }),
    ev(3, "sim_done", { ref: "S-1", hash: "h", label: "Base", ok: true, trust: { status: "ok" }, kind: "base", quality: "screen", measurements: { f_res_hz: { value: 2.4e9, unit: "Hz" } }, curve: null }),
  ]);
  assert.equal(s.sims["S-1"].status, "done");
  assert.equal(s.experiments.length, 1);
  assert.equal(s.experiments[0].measurements.f_res_hz.value, 2.4e9);
  assert.equal(s.experiments[0].curve, null, "no curve is invented when the backend sent none");
  assert.equal(s.experiments[0].suspect, "X-1", "each experiment knows which suspect it belongs to");
  assert.equal(s.experiments[0].hash, "h");
  assert.equal(s.sims["S-1"].pct, 1);
  assert.equal(s.trail.length, 1);
});

test("a running simulation shows its fraction and suspect before it is done", () => {
  const s = replayAll([
    ev(1, "sim_queued", { ref: "S-2", hash: "h2", label: "Test", kind: "whatif", quality: "screen", suspect: "X-2" }),
    ev(2, "sim_progress", { ref: "S-2", hash: "h2", fraction: 0.4, phase: "solving" }),
  ]);
  assert.equal(s.sims["S-2"].pct, 0.4);
  assert.equal(s.sims["S-2"].status, "running");
  assert.equal(s.sims["S-2"].suspect, "X-2");
  assert.equal(s.experiments.length, 0);
});

test("investigation and suspect events use their real keys", () => {
  const s = replayAll([
    ev(1, "investigation_updated", { investigation_id: "i", code: "I-001", question: "Q?", status: "running", suspects: [{ code: "S-1", title: "A", status: "pending" }] }),
    ev(2, "suspect_updated", { investigation_id: "i", suspect_id: "sid", code: "S-1", title: "A", status: "ruled_out", reason: "no effect" }),
    ev(3, "suspect_updated", { investigation_id: "nope", suspect_id: "x", code: "S-9", status: "confirmed" }),
  ]);
  assert.equal(s.investigations.i.suspects[0].status, "ruled_out");
  assert.equal(s.investigations.i.suspects[0].conclusion, "no effect");
  assert.equal(s.investigations.i.suspects.length, 1);
});

test("assumption_changed marks the affected findings by code, dismissed ones leave the counts", () => {
  let s = replayAll([ev(1, "finding_upserted", { finding: { ...F("h2", "medium"), code: "F-002" } }), ev(2, "finding_upserted", { finding: { ...F("h3", "low"), code: "F-003" } })]);
  s = reviewReducer(s, ev(3, "assumption_changed", { assumption: { id: "A-1", name: "dielectric_permittivity", value: 3.5, source: "user_provided" }, topic: "stackup", affected: [{ code: "F-002", status: "needs_recheck" }] }));
  assert.equal(s.findings.h2.status, "needs_recheck");
  assert.match(s.assumptions[0].text, /dielectric permittivity: 3.5/);
  const counts = severityCounts([{ ...s.findings.h2, status: "dismissed" }, s.findings.h3]);
  assert.equal(counts.medium, 0);
  assert.equal(counts.low, 1);
  assert.deepEqual(sortFindings(Object.values(s.findings)).map((f) => f.code), ["F-002", "F-003"]);
});

test("a thread streams deltas into one agent message, shows the approval card, and ends on completed", () => {
  let s = replayAll([
    ev(1, "user_message", { text: "hi", selected: [{ id: "ref:U3", type: "object" }], thread_id: "th" }),
    ev(2, "followup_started", { thread_id: "th", level: "answer", selected: [] }),
    ev(3, "text_delta", { text: "Hel" }, "t1"),
    ev(4, "text_delta", { text: "lo" }, "t1"),
    ev(5, "test_proposed", { thread_id: "th", title: "Clear pour", why: "y", estimated_seconds: 60 }),
    ev(6, "test", { thread_id: "th", title: "Clear pour", why: "y", estimated_seconds: 60, request_id: "r1" }),
  ], initialReviewState("thread"));
  assert.equal(s.followup.messages.at(-1).text, "Hello");
  assert.deepEqual(s.followup.messages[0].selected, ["ref:U3"]);
  assert.equal(s.followup.proposal.request_id, "r1", "the interaction event carries the id the page must answer");
  assert.equal(s.followup.proposal.status, "pending");
  assert.equal(s.followup.running, true);
  s = reviewReducer(s, ev(7, "request_resolved", { request_id: "r1", kind: "test", allow: false }));
  assert.equal(s.followup.proposal.status, "declined");
  s = reviewReducer(s, ev(8, "completed", { thread_id: "th", level: "answer", changes: 0 }));
  assert.equal(s.followup.running, false);
  assert.equal(s.status, "idle", "a thread ending does not end the review");
});

test("the review chat ignores text deltas and user_message (the agent's words arrive as thoughts)", () => {
  const s = replayAll([ev(1, "user_message", { kind: "review", goal: {} }), ev(2, "text_delta", { text: "x" })]);
  assert.equal(s.followup.messages.length, 0);
});

test("failed and canceled end the review with a reason", () => {
  assert.equal(replayAll([ev(1, "failed", { message: "boom" })]).failure, "boom");
  assert.equal(replayAll([ev(1, "canceled", {})]).status, "canceled");
  const restarted = replayAll([ev(1, "failed", { message: "Backend restarted; this run was interrupted.", interrupted: true })]);
  assert.equal(restarted.endState, "interrupted", "a restart mid-review is shown as interrupted, not as a failure or a finished review");
  assert.equal(replayAll([ev(1, "failed", { message: "boom" })]).endState, "pipeline_failed");
});

test("a finding changed by the user without a payload finding keeps its card", () => {
  let s = replayAll([ev(1, "finding_upserted", { finding: { ...F("h9", "low"), code: "F-009" } })]);
  s = reviewReducer(s, ev(2, "finding_changed", { finding_id: "h9", code: "F-009", status: "dismissed", source: "user" }));
  assert.equal(s.findings.h9.status, "dismissed");
  assert.equal(activeMarks(s).length, 0);
});

/* ---- refine rounds, cleared candidates, follow-up replies (payload shapes copied from a real Standard review and a real follow-up) ---- */

test("a refine round keeps the phase at 5 and says so; the next phase clears it", () => {
  const s = replayAll([
    ev(1, "phase_changed", { phase: 5, name: "confirm", previous: 4, skipped: [] }),
    ev(2, "phase_changed", { phase: 5, name: "confirm", previous: 5, substep: "refine", round: 1, rounds: 1, investigation: "I1", skipped: [] }),
  ]);
  assert.equal(s.phase, 5);
  assert.deepEqual(s.refine, { round: 1, rounds: 1, investigation: "I1" });
  const last = s.trail[s.trail.length - 1];
  assert.equal(last.text, "Refine round 1 of 1");
  assert.equal(last.substep, "refine");
  const done = reviewReducer(s, ev(3, "phase_changed", { phase: 6, name: "report", previous: 5, skipped: [] }));
  assert.equal(done.refine, null);
  assert.equal(done.phase, 6);
});

test("a suspect added by a refine round carries the round number", () => {
  let s = replayAll([ev(1, "investigation_updated", { investigation_id: "i", code: "I1", question: "q", status: "open", suspects: [{ code: "S1", title: "Antenna length detuned", status: "open" }] })]);
  s = reviewReducer(s, ev(2, "suspect_updated", { suspect_id: "x4", code: "S4", investigation_id: "i", title: "Refine round 1: combine a geometry change with matching", status: "open" }));
  const sus = s.investigations.i.suspects;
  assert.equal(sus.find((x) => x.code === "S1").round, undefined);
  assert.equal(sus.find((x) => x.code === "S4").round, 1);
});

test("dismissed rule groups are collected as Checked and cleared, with the reason and the count", () => {
  const s = replayAll([
    ev(1, "tool_start", { tool_call_id: "a", name: "dismiss_rule_group", input: { rule: "drc_hole_clearance", reason: "Same cause as the shorts." } }),
    ev(2, "tool_result", { tool_call_id: "a", name: "dismiss_rule_group", content: [{ type: "text", text: JSON.stringify({ dismissed_rule: "drc_hole_clearance", count: 19, reason: "Same cause as the shorts." }) }] }),
    ev(3, "tool_start", { tool_call_id: "b", name: "dismiss_candidate", input: { candidate_ids: ["candidate:x:1", "candidate:x:2"], reason: "Harmless pad overlap." } }),
    ev(4, "tool_result", { tool_call_id: "b", name: "dismiss_candidate", content: [{ type: "text", text: "ok" }] }),
  ]);
  assert.equal(s.cleared.length, 2);
  assert.deepEqual(s.cleared[0], { id: "tool-a", rule: "drc_hole_clearance", count: 19, reason: "Same cause as the shorts." });
  assert.equal(s.cleared[1].count, 2);
});

test("follow-up chat: narration before a tool stays apart from the reply streamed by end_followup", () => {
  const T = "t1";
  const s = replayAll([
    ev(1, "user_message", { text: "Why?", selected: [] }, T),
    ev(2, "text_delta", { text: "Let me look at the finding." }, T),
    ev(3, "tool_start", { tool_call_id: "g", name: "get_finding", input: { code: "F-009" } }, T),
    ev(4, "tool_result", { tool_call_id: "g", name: "get_finding", content: "x" }, T),
    ev(5, "tool_start", { tool_call_id: "e", name: "end_followup", input: { reply: "..." } }, T),
    ev(6, "text_delta", { text: "The antenna resonates at 1.87 GHz, " }, T),
    ev(7, "text_delta", { text: "far below the band." }, T),
    ev(8, "tool_result", { tool_call_id: "e", name: "end_followup", content: "{}" }, T),
    ev(9, "completed", {}, T),
  ].map((e) => e), initialReviewState("thread"));
  const agent = s.followup.messages.filter((m) => m.role === "agent");
  assert.equal(agent.length, 2);
  assert.equal(agent[0].text, "Let me look at the finding.");
  assert.equal(agent[0].narration, true);
  assert.equal(agent[1].text, "The antenna resonates at 1.87 GHz, far below the band.");
  assert.ok(!agent[1].narration);
  assert.equal(s.followup.running, false);
});

test("follow-up chat: text written after end_followup closed the reply is not a second copy", () => {
  const T = "t9";
  const s = replayAll([
    ev(1, "user_message", { text: "Why?", selected: [] }, T),
    ev(2, "tool_start", { tool_call_id: "e", name: "end_followup", input: { reply: "J1 fails." } }, T),
    ev(3, "text_delta", { text: "J1 fails because it faces into the board." }, T),
    ev(4, "tool_result", { tool_call_id: "e", name: "end_followup", content: "{}" }, T),
    ev(5, "text_delta", { text: "J" }, T),
    ev(6, "text_delta", { text: "1 fails because it faces into the board." }, T),
    ev(7, "thought", { text: "J1 fails because it faces into the board." }, T),
    ev(8, "completed", {}, T),
  ], initialReviewState("thread"));
  const agent = s.followup.messages.filter((m) => m.role === "agent");
  assert.equal(agent.length, 1);
  assert.equal(agent[0].text, "J1 fails because it faces into the board.");
  const next = replayAll([ev(9, "user_message", { text: "And?", selected: [] }, "t10"), ev(10, "text_delta", { text: "More." }, "t10")], s);
  assert.equal(next.followup.messages.at(-1).text, "More.", "a later turn still streams");
});

test("follow-up chat: a reply that only exists as a thought is still shown, and never twice", () => {
  const only = replayAll([ev(1, "user_message", { text: "q" }, "a"), ev(2, "thought", { text: "No. TrustPCB never edits a design." }, "a"), ev(3, "completed", {}, "a")], initialReviewState("thread"));
  assert.deepEqual(only.followup.messages.filter((m) => m.role === "agent").map((m) => m.text), ["No. TrustPCB never edits a design."]);
  const both = replayAll([ev(1, "user_message", { text: "q" }, "b"), ev(2, "text_delta", { text: "Same text." }, "b"), ev(3, "thought", { text: "Same text." }, "b")], initialReviewState("thread"));
  assert.equal(both.followup.messages.filter((m) => m.role === "agent").length, 1);
});

test("whatif_ready keeps the ghost the server sent", () => {
  const ghost = { ref: "C10", x: 26.34, y: 24.04, rotation: 0, side: "top", ring: [[25.43, 23.58], [27.25, 23.58], [27.25, 24.5], [25.43, 24.5]], pads: [] };
  const s = replayAll([ev(1, "whatif_ready", { ref: "C10", ghost, new_position: [26.34, 24.04], distances: [{ to: "ref:U3", distance_mm: 0.25, was_mm: 7.194 }], conflicts: [], label: "What-if, not your board" })], initialReviewState("thread"));
  assert.equal(s.followup.whatif.ghost.ring.length, 4);
  assert.equal(s.followup.whatif.label, "What-if, not your board");
});

test("skipped phases keep the server's reason, with a capital letter, for the investigation view", () => {
  let s = initialReviewState();
  s = reviewReducer(s, ev(1, "phase_changed", { phase: 6, name: "report", previous: 3, skipped: [
    { phase: 4, name: "investigate", reason: "no wireless part or antenna was found on this board" },
    { phase: 5, name: "confirm", reason: "No investigation found an improvement to confirm." }] }));
  assert.deepEqual(s.skipped.map((x) => x.phase), [4, 5]);
  assert.equal(s.skipped[0].reason, "No wireless part or antenna was found on this board");
  const row = s.trail.find((e) => e.text === "Skipped investigate");
  assert.equal(row.detail, "No wireless part or antenna was found on this board");
  assert.deepEqual(initialReviewState().skipped, []);
  const src = fs.readFileSync(new URL("../components/workspace/InvestigationBoard.tsx", import.meta.url), "utf8");
  assert.match(src, /data-testid="no-investigation"/, "the board explains a skipped investigation instead of promising one");
});
