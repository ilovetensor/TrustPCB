// Pure geometry helpers: target resolution, view maths, ruler ticks, callout layout.
// All board units are millimetres; screen units are CSS pixels.

/** @typedef {[number, number]} Pt */

export function bboxOfPoints(points) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of points) {
    if (x < x0) x0 = x;
    if (y < y0) y0 = y;
    if (x > x1) x1 = x;
    if (y > y1) y1 = y;
  }
  return [x0, y0, x1, y1];
}

export function bboxUnion(boxes) {
  const valid = boxes.filter((b) => b && isFinite(b[0]));
  if (!valid.length) return null;
  return [
    Math.min(...valid.map((b) => b[0])),
    Math.min(...valid.map((b) => b[1])),
    Math.max(...valid.map((b) => b[2])),
    Math.max(...valid.map((b) => b[3])),
  ];
}

export function bboxCenter(b) {
  return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
}

export function padBBox(b, m) {
  return [b[0] - m, b[1] - m, b[2] + m, b[3] + m];
}

/** Sample a three-point arc (start, mid, end) into a polyline. */
export function arcPoints(a, m, b, steps = 12) {
  const [ax, ay] = a, [mx, my] = m, [bx, by] = b;
  const d = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my));
  if (Math.abs(d) < 1e-9) return [a, b];
  const ux = ((ax ** 2 + ay ** 2) * (my - by) + (mx ** 2 + my ** 2) * (by - ay) + (bx ** 2 + by ** 2) * (ay - my)) / d;
  const uy = ((ax ** 2 + ay ** 2) * (bx - mx) + (mx ** 2 + my ** 2) * (ax - bx) + (bx ** 2 + by ** 2) * (mx - ax)) / d;
  const r = Math.hypot(ax - ux, ay - uy);
  const norm = (v) => ((v % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
  const a0 = Math.atan2(ay - uy, ax - ux);
  let sweep = norm(Math.atan2(by - uy, bx - ux) - a0);
  const midSweep = norm(Math.atan2(my - uy, mx - ux) - a0);
  if (midSweep > sweep) sweep -= 2 * Math.PI;
  const out = [];
  for (let i = 0; i <= steps; i++) {
    const t = a0 + (sweep * i) / steps;
    out.push([ux + r * Math.cos(t), uy + r * Math.sin(t)]);
  }
  return out;
}

/** Target ids name nets as `net:<name>`; board.json from the server writes the bare name on tracks and pads. */
export function netId(name) {
  return name == null ? null : name.startsWith("net:") ? name : `net:${name}`;
}

/** Index a board once for O(1) target lookups. */
export function indexBoard(board) {
  const byId = new Map();
  const netTracks = new Map();
  const netPads = new Map();
  const netVias = new Map();
  for (const f of board.footprints) byId.set(f.id, f);
  for (const p of board.pads) {
    byId.set(p.id, p);
    if (p.net) (netPads.get(p.net) ?? netPads.set(p.net, []).get(p.net)).push(p);
  }
  for (const t of board.tracks) {
    byId.set(t.id, t);
    if (t.net) (netTracks.get(t.net) ?? netTracks.set(t.net, []).get(t.net)).push(t);
  }
  for (const v of board.vias) {
    byId.set(v.id, v);
    if (v.net) (netVias.get(v.net) ?? netVias.set(v.net, []).get(v.net)).push(v);
  }
  for (const z of board.zones) byId.set(z.id, z);
  for (const k of board.keepouts) byId.set(k.id, k);
  return { board, byId, netTracks, netPads, netVias };
}

function trackPoints(t) {
  return t.mid ? arcPoints(t.start, t.mid, t.end) : [t.start, t.end];
}

/**
 * Resolve a server-issued target id into geometry (contracts.md `Board.resolve`).
 * Returns null when the id is unknown or not resolvable on the client.
 */
export function resolveTarget(index, id) {
  const { board } = index;
  if (id.startsWith("net:")) {
    const parts = [];
    for (const t of index.netTracks.get(id) ?? []) {
      const pts = trackPoints(t);
      parts.push({ kind: "polyline", points: pts, bbox: bboxOfPoints(pts), layer: t.layer });
    }
    for (const p of index.netPads.get(id) ?? [])
      parts.push({ kind: "polygon", rings: [p.ring], bbox: bboxOfPoints(p.ring), layer: p.layers?.[0] });
    if (!parts.length) return null;
    return { kind: "group", parts, bbox: bboxUnion(parts.map((p) => p.bbox)) };
  }
  const o = index.byId.get(id);
  if (!o) return null;
  if (id.startsWith("ref:")) {
    const ring = o.courtyard ?? [[o.bbox[0], o.bbox[1]], [o.bbox[2], o.bbox[1]], [o.bbox[2], o.bbox[3]], [o.bbox[0], o.bbox[3]]];
    return { kind: "polygon", rings: [ring], bbox: o.bbox, side: o.side };
  }
  if (id.startsWith("pad:"))
    return { kind: "polygon", rings: [o.ring], bbox: bboxOfPoints(o.ring), layer: o.layers?.[0] };
  if (id.startsWith("track:")) {
    const pts = trackPoints(o);
    return { kind: "polyline", points: pts, bbox: bboxOfPoints(pts), layer: o.layer };
  }
  if (id.startsWith("via:")) {
    const r = o.diameter / 2;
    return { kind: "point", points: [[o.x, o.y]], radius: r, bbox: [o.x - r, o.y - r, o.x + r, o.y + r] };
  }
  if (id.startsWith("zone:")) {
    const rings = Object.values(o.filled).flat();
    const pts = rings.flat();
    return { kind: "polygon", rings, bbox: bboxOfPoints(pts), layer: o.layers?.[0] };
  }
  if (id.startsWith("keepout:"))
    return { kind: "polygon", rings: [o.ring], bbox: bboxOfPoints(o.ring), layer: o.layers?.[0] };
  return null;
}

export function geometryCenter(g) {
  return bboxCenter(g.bbox);
}

/** Resolve all targets of a mark. Uses server `resolved` when present. */
export function resolveMark(index, mark) {
  if (mark.resolved?.length) return mark.resolved;
  return mark.targets.map((t) => resolveTarget(index, t)).filter(Boolean);
}

export function pointOnPolyline(points, t = 0.5) {
  let total = 0;
  const segs = [];
  for (let i = 1; i < points.length; i++) {
    const l = Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
    segs.push(l);
    total += l;
  }
  let want = total * t;
  for (let i = 0; i < segs.length; i++) {
    if (want <= segs[i] || i === segs.length - 1) {
      const k = segs[i] === 0 ? 0 : Math.min(1, want / segs[i]);
      return [points[i][0] + (points[i + 1][0] - points[i][0]) * k, points[i][1] + (points[i + 1][1] - points[i][1]) * k];
    }
    want -= segs[i];
  }
  return points[0];
}

/** Where a callout leader should land for a mark. Board mm. */
export function markAnchor(mark, geoms) {
  if (!geoms.length) return null;
  const g = geoms[0];
  if (mark.type === "link" && geoms.length > 1) {
    const a = bboxCenter(geoms[0].bbox);
    const b = bboxCenter(geoms[1].bbox);
    return [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  }
  if (mark.type === "path" || (g.kind === "group" && g.parts)) {
    const lines = (g.parts ?? [g]).filter((p) => p.kind === "polyline");
    if (lines.length) {
      // the longest polyline's midpoint reads best
      let best = lines[0], bl = -1;
      for (const l of lines) {
        const len = l.points.reduce((s, p, i) => (i ? s + Math.hypot(p[0] - l.points[i - 1][0], p[1] - l.points[i - 1][1]) : 0), 0);
        if (len > bl) { bl = len; best = l; }
      }
      return pointOnPolyline(best.points, 0.5);
    }
  }
  if (g.kind === "point") return g.points[0];
  if (g.kind === "polyline") return pointOnPolyline(g.points, 0.5);
  if (mark.type === "region") return [g.bbox[2], g.bbox[1]];
  return bboxCenter(g.bbox);
}

/** Merge several geometries' bbox, padded. */
export function geometriesBBox(geoms) {
  return bboxUnion(geoms.map((g) => g.bbox));
}

/* ----------------------------- view maths ----------------------------- */

/** View: screen = board * scale + (tx, ty). */
export function fitView(bbox, width, height, pad = { l: 60, r: 60, t: 60, b: 60 }) {
  const bw = Math.max(1e-6, bbox[2] - bbox[0]);
  const bh = Math.max(1e-6, bbox[3] - bbox[1]);
  const aw = Math.max(10, width - pad.l - pad.r);
  const ah = Math.max(10, height - pad.t - pad.b);
  const scale = Math.min(aw / bw, ah / bh);
  const cx = (bbox[0] + bbox[2]) / 2;
  const cy = (bbox[1] + bbox[3]) / 2;
  return {
    scale,
    tx: pad.l + aw / 2 - cx * scale,
    ty: pad.t + ah / 2 - cy * scale,
  };
}

export function toScreen(view, x, y) {
  return [x * view.scale + view.tx, y * view.scale + view.ty];
}

export function toBoard(view, sx, sy) {
  return [(sx - view.tx) / view.scale, (sy - view.ty) / view.scale];
}

export function zoomAt(view, sx, sy, factor, min = 0.5, max = 400) {
  const next = Math.min(max, Math.max(min, view.scale * factor));
  const k = next / view.scale;
  return { scale: next, tx: sx - (sx - view.tx) * k, ty: sy - (sy - view.ty) * k };
}

/* ------------------------------- rulers ------------------------------- */

const STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500];

/** Pick major and minor spacing (mm) so majors are at least `minPx` apart. */
export function rulerSteps(pxPerMm, minPx = 70) {
  let major = STEPS[STEPS.length - 1];
  for (const s of STEPS) {
    if (s * pxPerMm >= minPx) { major = s; break; }
  }
  const mant = Number((major / 10 ** Math.floor(Math.log10(major))).toFixed(3));
  const minor = mant === 1 ? major / 10 : mant === 2 ? major / 4 : major / 5;
  return { major, minor: Math.max(minor, 0.01) };
}

/** Tick positions in mm across [min,max] for a step. */
export function tickRange(min, max, step) {
  const out = [];
  const first = Math.ceil(min / step - 1e-9);
  const last = Math.floor(max / step + 1e-9);
  for (let i = first; i <= last; i++) out.push(+(i * step).toFixed(6));
  return out;
}

export function formatMm(v, step) {
  const d = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
  return v.toFixed(d);
}

/* ------------------------- clustering and callouts ------------------------- */

/**
 * Cluster nearby screen anchors. `keep` ids are never merged (selected items).
 * Returns [{ ids, ax, ay, count }].
 */
export function clusterAnchors(items, radius, keep = new Set()) {
  const parent = items.map((_, i) => i);
  const find = (i) => (parent[i] === i ? i : (parent[i] = find(parent[i])));
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length; j++) {
      if (keep.has(items[i].id) || keep.has(items[j].id)) continue;
      if (Math.hypot(items[i].ax - items[j].ax, items[i].ay - items[j].ay) < radius) parent[find(i)] = find(j);
    }
  }
  const groups = new Map();
  items.forEach((it, i) => {
    const r = find(i);
    (groups.get(r) ?? groups.set(r, []).get(r)).push(it);
  });
  return [...groups.values()].map((g) => ({
    ids: g.map((x) => x.id),
    members: g,
    ax: g.reduce((s, x) => s + x.ax, 0) / g.length,
    ay: g.reduce((s, x) => s + x.ay, 0) / g.length,
    count: g.length,
    priority: Math.max(...g.map((x) => x.priority ?? 0)),
  }));
}

function segIntersect(p1, p2, p3, p4) {
  const d = (p2[0] - p1[0]) * (p4[1] - p3[1]) - (p2[1] - p1[1]) * (p4[0] - p3[0]);
  if (Math.abs(d) < 1e-9) return false;
  const t = ((p3[0] - p1[0]) * (p4[1] - p3[1]) - (p3[1] - p1[1]) * (p4[0] - p3[0])) / d;
  const u = ((p3[0] - p1[0]) * (p2[1] - p1[1]) - (p3[1] - p1[1]) * (p2[0] - p1[0])) / d;
  return t > 0.02 && t < 0.98 && u > 0.02 && u < 0.98;
}

function rectHitsCircle(r, cx, cy, rad) {
  const nx = Math.max(r.x, Math.min(cx, r.x + r.w));
  const ny = Math.max(r.y, Math.min(cy, r.y + r.h));
  return Math.hypot(cx - nx, cy - ny) < rad;
}

function segHitsRect(a, b, r) {
  const c = [[r.x, r.y], [r.x + r.w, r.y], [r.x + r.w, r.y + r.h], [r.x, r.y + r.h]];
  for (let i = 0; i < 4; i++) if (segIntersect(a, b, c[i], c[(i + 1) % 4])) return true;
  return a[0] > r.x && a[0] < r.x + r.w && a[1] > r.y && a[1] < r.y + r.h;
}

const DIRS = [-45, 45, -135, 135, -90, 0, 180, 90].map((d) => (d * Math.PI) / 180);
const DISTS = [42, 66, 94, 128];

/**
 * Place numbered balloons around anchors, avoiding each other, other leaders,
 * the viewport edge and obstacle rectangles (floating panels).
 * items: [{id, ax, ay, priority?}]; prev: Map id -> {bx,by} for hysteresis.
 * Returns Map id -> {bx, by, ex, ey, sx, sy} where the leader is
 * anchor -> elbow (ex,ey) -> balloon edge (sx,sy).
 */
export function layoutCallouts(items, { width, height, obstacles = [], radius = 12, prev = new Map(), margin = 8 } = {}) {
  const order = [...items].sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0) || a.ax - b.ax);
  const placed = new Map();
  const leaders = [];
  const balloons = [];
  for (const it of order) {
    let best = null;
    const before = prev.get(it.id);
    for (let di = 0; di < DISTS.length; di++) {
      for (let k = 0; k < DIRS.length; k++) {
        const ang = DIRS[k];
        const bx = it.ax + Math.cos(ang) * DISTS[di];
        const by = it.ay + Math.sin(ang) * DISTS[di];
        let cost = di * 14 + k * 5;
        if (bx < margin + radius || bx > width - margin - radius || by < margin + radius || by > height - margin - radius) cost += 400;
        for (const o of obstacles) if (rectHitsCircle(o, bx, by, radius + 4)) cost += 500;
        for (const b of balloons) {
          const d = Math.hypot(b.bx - bx, b.by - by);
          if (d < radius * 2 + 8) cost += 600 + (radius * 2 + 8 - d) * 8;
        }
        // leader: anchor -> elbow
        const s = Math.sign(bx - it.ax) || 1;
        const ex = bx - s * (radius + 10);
        const ey = by;
        for (const l of leaders) {
          if (segIntersect([it.ax, it.ay], [ex, ey], l.a, l.e)) cost += 220;
          if (segIntersect([ex, ey], [bx, by], l.a, l.e)) cost += 120;
        }
        // do not run a leader through someone else's balloon
        for (const b of balloons) {
          const dx = ex - it.ax, dy = ey - it.ay;
          const len2 = dx * dx + dy * dy || 1;
          const t = Math.max(0, Math.min(1, ((b.bx - it.ax) * dx + (b.by - it.ay) * dy) / len2));
          if (Math.hypot(it.ax + dx * t - b.bx, it.ay + dy * t - b.by) < radius + 2) cost += 260;
        }
        for (const o of obstacles) if (segHitsRect([it.ax, it.ay], [ex, ey], o)) cost += 160;
        // other anchors under this balloon
        for (const other of items) {
          if (other.id === it.id) continue;
          if (Math.hypot(other.ax - bx, other.ay - by) < radius + 3) cost += 140;
        }
        if (before) {
          const moved = Math.hypot(before.bx - bx, before.by - by);
          cost += Math.min(moved, 200) * 0.25 - (moved < 6 ? 40 : 0);
        }
        if (!best || cost < best.cost) best = { cost, bx, by, ex, ey, s };
      }
    }
    placed.set(it.id, {
      bx: best.bx, by: best.by, ex: best.ex, ey: best.ey,
      sx: best.bx - best.s * radius, sy: best.by,
    });
    balloons.push({ bx: best.bx, by: best.by });
    leaders.push({ a: [it.ax, it.ay], e: [best.ex, best.ey] });
  }
  return placed;
}

/** Arrowhead polygon points at `tip` pointing along direction (dx,dy). */
export function arrowhead(tip, dx, dy, len = 7, half = 2.2) {
  const l = Math.hypot(dx, dy) || 1;
  const ux = dx / l, uy = dy / l;
  const bx = tip[0] - ux * len, by = tip[1] - uy * len;
  return [tip, [bx - uy * half, by + ux * half], [bx + uy * half, by - ux * half]];
}

/* --------------------------- nearest pair (links) --------------------------- */

/** Flatten a geometry to line segments [[x,y],[x,y]] (points become degenerate). */
export function geometrySegments(g) {
  const out = [];
  const walk = (x) => {
    if (x.parts) x.parts.forEach(walk);
    if (x.kind === "point") out.push([x.points[0], x.points[0]]);
    else if (x.kind === "polyline") for (let i = 1; i < x.points.length; i++) out.push([x.points[i - 1], x.points[i]]);
    else if (x.kind === "polygon")
      for (const ring of x.rings) for (let i = 0; i < ring.length; i++) out.push([ring[i], ring[(i + 1) % ring.length]]);
  };
  walk(g);
  return out;
}

function closestOnSeg(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy;
  const t = l2 === 0 ? 0 : Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2));
  return [a[0] + dx * t, a[1] + dy * t];
}

/** Shortest segment joining two geometries: { a, b, d } in mm (a on ga, b on gb). */
export function nearestPair(ga, gb) {
  const sa = geometrySegments(ga);
  const sb = geometrySegments(gb);
  let best = null;
  const consider = (a, b) => {
    const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
    if (!best || d < best.d) best = { a, b, d };
  };
  for (const [a0, a1] of sa) {
    for (const [b0, b1] of sb) {
      consider(a0, closestOnSeg(a0, b0, b1));
      consider(a1, closestOnSeg(a1, b0, b1));
      consider(closestOnSeg(b0, a0, a1), b0);
      consider(closestOnSeg(b1, a0, a1), b1);
    }
  }
  return best;
}

/** Dimension line geometry in screen space: extension lines, line, arrows, label anchor. */
export function dimensionGeometry(a, b, offset = 16) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const len = Math.hypot(dx, dy) || 1;
  // normal pointing up-ish so labels read above the line
  let nx = -dy / len, ny = dx / len;
  if (ny > 0 || (ny === 0 && nx > 0)) { nx = -nx; ny = -ny; }
  const a2 = [a[0] + nx * offset, a[1] + ny * offset];
  const b2 = [b[0] + nx * offset, b[1] + ny * offset];
  const ux = dx / len, uy = dy / len;
  const inside = len >= 36;
  return {
    ext: [[a, [a2[0] + nx * 5, a2[1] + ny * 5]], [b, [b2[0] + nx * 5, b2[1] + ny * 5]]],
    line: inside ? [a2, b2] : [[a2[0] - ux * 12, a2[1] - uy * 12], [b2[0] + ux * 12, b2[1] + uy * 12]],
    arrows: inside
      ? [arrowhead(a2, -ux, -uy), arrowhead(b2, ux, uy)]
      : [arrowhead(a2, ux, uy), arrowhead(b2, -ux, -uy)],
    mid: [(a2[0] + b2[0]) / 2 + nx * 9, (a2[1] + b2[1]) / 2 + ny * 9],
    len,
    inside,
  };
}
