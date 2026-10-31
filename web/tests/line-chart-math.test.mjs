import { test } from "node:test";
import assert from "node:assert/strict";
import { impedanceRange, clipCurve } from "../lib/line-chart-math.mjs";

test("the range follows the line's section values and the target, not the open end", () => {
  assert.deepEqual(impedanceRange([48, 52, 55], 50, 10), [0, 100]);
  assert.deepEqual(impedanceRange([48, 90, 110], 50, 10), [0, 175]);
  assert.deepEqual(impedanceRange([400, 900], 50), [0, 200], "never above 200");
  assert.deepEqual(impedanceRange([], null), [0, 150]);
  assert.deepEqual(impedanceRange([30, 34], null), [0, 100], "no target: the values decide, never below 100");
});

test("a TDR that ramps to kilo-ohms is held in the range and the open end is where it leaves it", () => {
  const xs = [0, 1, 2, 3, 4], ys = [50, 52, 60, 400, 10000];
  const { shown, exitX, peak } = clipCurve(xs, ys, 0, 100);
  assert.deepEqual(shown, [50, 52, 60, 100, 100]);
  assert.ok(exitX > 2 && exitX < 3, `the line leaves the range between 60 and 400 ohm: ${exitX}`);
  assert.equal(peak, 10000);
  assert.equal(clipCurve(xs, [50, 51, 52, 53, 54], 0, 100).exitX, null, "a line inside the range has no open end");
});
