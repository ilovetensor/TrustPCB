import { test } from "node:test";
import assert from "node:assert/strict";
import { endStateText, END_STATE_TEXT } from "../lib/end-state.mjs";
import { replayAll } from "../lib/review-reducer.mjs";

const ev = (sequence, type, payload) => ({ sequence, type, payload, turn_id: "t", created_at: "2026-10-31T00:00:00Z" });

test("an agent that stopped early is never called complete", () => {
  assert.equal(endStateText("agent_stopped"), "Stopped early");
  assert.equal(endStateText("complete"), "Complete");
  assert.equal(endStateText("complete_with_limits"), "Complete, limits");
  assert.equal(endStateText("canceled"), "Stopped");
  assert.equal(endStateText("pipeline_failed"), "Failed");
  assert.equal(endStateText(null, "Loading"), "Loading");
  assert.ok(!Object.values(END_STATE_TEXT).some((t) => t === "Agent stopped"));
});

test("the review state keeps the end state the server gave, so the sidebar can say it", () => {
  const s = replayAll([ev(1, "user_message", { kind: "review" }), ev(2, "completed", { end_state: "agent_stopped" })]);
  assert.equal(s.status, "complete", "the stream finished");
  assert.equal(s.endState, "agent_stopped");
  assert.equal(endStateText(s.endState, "Complete"), "Stopped early");
});
