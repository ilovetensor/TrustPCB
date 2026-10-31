import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {
  indexBoard, resolveTarget, fitView, toScreen, toBoard, zoomAt, rulerSteps, tickRange,
  clusterAnchors, layoutCallouts, markAnchor, resolveMark, arcPoints,
} from "../lib/geometry.mjs";

const board = JSON.parse(fs.readFileSync(new URL("../public/fixtures/nrf/board.json", import.meta.url)));
const idx = indexBoard(board);

test("resolves each contract target kind", () => {
  assert.equal(resolveTarget(idx, "ref:L1").kind, "polygon");
  assert.equal(resolveTarget(idx, "pad:U3:A22").kind, "polygon");
  assert.equal(resolveTarget(idx, "net:ANT_FEED").kind, "group");
  assert.equal(resolveTarget(idx, "keepout:1444dc6c").kind, "polygon");
  const via = resolveTarget(idx, board.vias[0].id);
  assert.equal(via.kind, "point");
  assert.equal(resolveTarget(idx, "track:00000000"), null);
});

test("net bbox covers every track of the net", () => {
  const g = resolveTarget(idx, "net:ANT_FEED");
  for (const t of board.tracks.filter((t) => t.net === "net:ANT_FEED")) {
    assert.ok(g.bbox[0] <= Math.min(t.start[0], t.end[0]) + 1e-9);
    assert.ok(g.bbox[3] >= Math.max(t.start[1], t.end[1]) - 1e-9);
  }
});

test("view round trip and zoom keep the cursor point fixed", () => {
  const v = fitView(board.bbox, 1000, 600);
  const [sx, sy] = toScreen(v, 33, 26);
  const [bx, by] = toBoard(v, sx, sy);
  assert.ok(Math.abs(bx - 33) < 1e-9 && Math.abs(by - 26) < 1e-9);
  const z = zoomAt(v, 400, 300, 2);
  const before = toBoard(v, 400, 300);
  const after = toBoard(z, 400, 300);
  assert.ok(Math.abs(before[0] - after[0]) < 1e-9);
  assert.ok(Math.abs(before[1] - after[1]) < 1e-9);
});

test("ruler steps adapt to zoom", () => {
  assert.equal(rulerSteps(4).major, 20);
  assert.equal(rulerSteps(40).major, 2);
  assert.equal(rulerSteps(400).major, 0.2);
  assert.deepEqual(tickRange(-0.5, 5.5, 2), [0, 2, 4]);
});

test("arc sampling passes through the middle point", () => {
  const pts = arcPoints([0, 2], [0.585786, 0.585786], [2, 0], 8);
  assert.equal(pts.length, 9);
  assert.ok(pts.some((p) => Math.hypot(p[0] - 0.585786, p[1] - 0.585786) < 0.12));
});

test("clusterAnchors merges close anchors but keeps selected", () => {
  const items = [
    { id: "a", ax: 0, ay: 0 }, { id: "b", ax: 5, ay: 3 }, { id: "c", ax: 200, ay: 200 }, { id: "d", ax: 3, ay: 1 },
  ];
  const clusters = clusterAnchors(items, 20);
  assert.equal(clusters.length, 2);
  assert.equal(clusterAnchors(items, 20, new Set(["d"])).length, 3);
});

test("layoutCallouts avoids overlap, obstacles and the viewport edge", () => {
  const items = [];
  for (let i = 0; i < 9; i++) items.push({ id: "f" + i, ax: 300 + (i % 3) * 14, ay: 200 + Math.floor(i / 3) * 12, priority: 9 - i });
  const obstacles = [{ x: 0, y: 0, w: 120, h: 600 }];
  const out = layoutCallouts(items, { width: 800, height: 600, obstacles });
  const arr = [...out.entries()];
  assert.equal(arr.length, 9);
  for (let i = 0; i < arr.length; i++) {
    const a = arr[i][1];
    assert.ok(a.bx > 20 && a.bx < 780 && a.by > 20 && a.by < 580);
    assert.ok(a.bx > 120 + 12, "balloon clear of panel");
    for (let j = i + 1; j < arr.length; j++) {
      const b = arr[j][1];
      assert.ok(Math.hypot(a.bx - b.bx, a.by - b.by) >= 24, "balloons do not overlap");
    }
  }
});

test("markAnchor picks sensible anchors", () => {
  const link = { type: "link", targets: ["ref:C12", "pad:U3:A22"] };
  const geoms = resolveMark(idx, link);
  assert.equal(geoms.length, 2);
  const a = markAnchor(link, geoms);
  assert.ok(a[0] > 30 && a[0] < 40);
  const path = { type: "path", targets: ["net:ANT_FEED"] };
  const p = markAnchor(path, resolveMark(idx, path));
  assert.ok(isFinite(p[0]) && isFinite(p[1]));
});

import { nearestPair, dimensionGeometry } from "../lib/geometry.mjs";
test("nearestPair finds the shortest gap between a track and the keep-clear", () => {
  const a = resolveTarget(idx, "track:05bc5591");
  const b = resolveTarget(idx, "keepout:1444dc6c");
  const np = nearestPair(a, b);
  assert.ok(np.d > 0 && np.d < 1);
  assert.ok(Math.abs(np.b[1] - 10) < 1e-6, "lands on the y=10 edge of the keep-clear");
});
test("dimensionGeometry keeps arrows inside for long spans", () => {
  const d = dimensionGeometry([0, 0], [100, 0]);
  assert.equal(d.inside, true);
  assert.equal(d.arrows.length, 2);
  assert.ok(d.mid[1] < 0, "label sits above the line");
});

import { netId } from "../lib/geometry.mjs";
test("net names from the server become target ids once", () => {
  assert.equal(netId("3V3"), "net:3V3");
  assert.equal(netId("net:GND"), "net:GND");
  assert.equal(netId(null), null);
});
