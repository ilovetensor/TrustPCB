import { test } from "node:test";
import assert from "node:assert/strict";
import { initialReviewState, reviewReducer, replayAll } from "../lib/review-reducer.mjs";
import { allowedRoute } from "../lib/proxy-policy.mjs";

const ev = (sequence, type, payload) => ({ sequence, type, payload, turn_id: "t", created_at: "2026-10-31T00:00:00Z" });

test("a review waiting for a review slot shows its place, and it clears when the review starts working", () => {
  let s = replayAll([ev(1, "user_message", { kind: "review" }), ev(2, "queued", { position: 2, ahead: 4, working: 3, limit: 3, message: "Waiting for a free review slot: 3 of 3 reviews are working, 1 waiting ahead." })]);
  assert.equal(s.status, "running", "a queued review is live: Stop and the lists treat it as running");
  assert.equal(s.queue.position, 2);
  assert.match(s.queue.message, /Waiting for a free review slot/);
  s = reviewReducer(s, ev(3, "queued", { position: 1, ahead: 3, working: 3, limit: 3, message: "Waiting for a free review slot: 3 of 3 reviews are working, 0 waiting ahead." }));
  assert.equal(s.queue.position, 1);
  s = reviewReducer(s, ev(4, "phase_changed", { phase: 0, name: "Intake", previous: null }));
  assert.equal(s.queue, null);
  assert.equal(s.trail.filter((t) => t.id === "queue-wait").length, 1, "the queue line is one trail entry that updates");
});

test("an interrupted review (the app stopped) is not shown as stopped by the user", () => {
  const s = replayAll([ev(1, "phase_changed", { phase: 2, name: "Understand", previous: 1 }), ev(2, "failed", { message: "The app stopped", interrupted: true })]);
  assert.equal(s.endState, "interrupted");
});

test("a simulation waiting for the solver reports its place and the time budget says the wait is free", () => {
  let s = replayAll([ev(1, "phase_changed", { phase: 4, name: "Investigate", previous: 3 }), ev(2, "sim_queued", { ref: "R1", label: "base", kind: "base", quality: "screen", hash: "h" }),
    ev(3, "sim_progress", { ref: "R1", hash: "h", phase: "waiting", position: 1, ahead: 1, fraction: 0 }),
    ev(4, "budget", { wall_s: { used: 10, limit: 3600, left: 3590 }, solver_wait_s: { used: 300, cap: 14400, waiting: true } })]);
  assert.deepEqual(s.sims.R1.waiting, { position: 1, ahead: 1 });
  assert.equal(s.budget.solver_wait_s.waiting, true);
  s = reviewReducer(s, ev(5, "sim_progress", { ref: "R1", hash: "h", phase: "started", fraction: 0 }));
  assert.equal(s.sims.R1.waiting, null);
});

test("the proxy lets the page read the running list and restart an interrupted review, nothing else new", () => {
  assert.ok(allowedRoute("GET", "runs"));
  assert.ok(allowedRoute("POST", "reviews/r-1/restart"));
  assert.equal(allowedRoute("GET", "reviews/r-1/restart"), false);
  assert.equal(allowedRoute("POST", "runs"), false);
});
