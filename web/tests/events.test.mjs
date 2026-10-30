import { test } from "node:test";
import assert from "node:assert/strict";
import { mergeEvents, readFrames, isReviewRunning } from "../lib/events.mjs";
test("mergeEvents dedupes by sequence and sorts", () => {
  const merged = mergeEvents(
    [{ sequence: 2, type: "a" }, { sequence: 1, type: "b" }],
    [{ sequence: 2, type: "c" }, { sequence: 3, type: "d" }],
  );
  assert.deepEqual(merged.map((e) => [e.sequence, e.type]), [[1, "b"], [2, "c"], [3, "d"]]);
});
test("readFrames splits SSE frames and keeps the partial tail", () => {
  const { events, rest } = readFrames('data: {"sequence":1}\n\ndata: {"sequence":2}\n\ndata: {"seq');
  assert.equal(events.length, 2);
  assert.equal(rest, 'data: {"seq');
});
test("isReviewRunning follows status and terminal events", () => {
  assert.equal(isReviewRunning([{ type: "phase_changed", payload: {} }]), true);
  assert.equal(isReviewRunning([{ type: "phase_changed", payload: {} }, { type: "completed", payload: {} }]), false);
  assert.equal(isReviewRunning([], "waiting_for_approval"), true);
});
