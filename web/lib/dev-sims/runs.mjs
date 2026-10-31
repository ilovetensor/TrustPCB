// Dev fixture runs for the simulation lab, in the shape of the simulators' result.json. Built on a real board (the bundled nRF52840 sample)
// by the small solvers in engine.mjs, so the maps and numbers hang together. Never shipped: see lib/dev-sims-loader.ts.

import { arcPoints, boxBlur, copperMask, CU_T_MM, distanceTo, fillRings, makeGrid, microstrip, RHO_S_1OZ, solveCopper, solvePlate, tdr } from "./engine.mjs";

const FIXTURE = "Dev fixture value, not from a datasheet";
const asm = (name, value, unit, source, cite) => ({ name, value, unit, source, ...(cite ? { cite } : {}) });
const pct = (a, q) => { const s = Float32Array.from(a.filter(Number.isFinite)).sort(); return s.length ? s[Math.min(s.length - 1, Math.floor(q * s.length))] : 0; };
const finiteMax = (a) => { let m = -Infinity; for (const v of a) if (Number.isFinite(v) && v > m) m = v; return m; };
const finiteMin = (a) => { let m = Infinity; for (const v of a) if (Number.isFinite(v) && v < m) m = v; return m; };
const gridOf = (g) => ({ x0: g.x0, y0: g.y0, dx: g.dx, dy: g.dx, nx: g.nx, ny: g.ny });
const padOf = (board, ref, number) => board.pads.filter((p) => p.ref === ref && (number == null || p.number === number));
const nid = (n) => `net:${n}`;

/** The cells of a pad on a layer: [index, ...] on the main grid. */
function padCells(board, g, pad) {
  const xs = pad.ring.map((p) => p[0]), ys = pad.ring.map((p) => p[1]);
  const i0 = Math.max(0, Math.floor((Math.min(...xs) - g.x0) / g.dx)), i1 = Math.min(g.nx - 1, Math.floor((Math.max(...xs) - g.x0) / g.dx));
  const j0 = Math.max(0, Math.floor((Math.min(...ys) - g.y0) / g.dx)), j1 = Math.min(g.ny - 1, Math.floor((Math.max(...ys) - g.y0) / g.dx));
  const sub = { x0: g.x0 + i0 * g.dx, y0: g.y0 + j0 * g.dx, dx: g.dx, nx: i1 - i0 + 1, ny: j1 - j0 + 1 };
  const buf = new Uint8Array(sub.nx * sub.ny);
  fillRings(buf, sub, [pad.ring]);
  const out = [];
  for (let j = 0; j < sub.ny; j++) for (let i = 0; i < sub.nx; i++) if (buf[j * sub.nx + i]) out.push((j0 + j) * g.nx + i0 + i);
  if (!out.length) out.push(Math.min(g.ny - 1, Math.max(0, Math.floor((pad.y - g.y0) / g.dx))) * g.nx + Math.min(g.nx - 1, Math.max(0, Math.floor((pad.x - g.x0) / g.dx))));
  return out;
}
const meanAt = (data, cells) => { let s = 0, n = 0; for (const k of cells) if (Number.isFinite(data[k])) { s += data[k]; n++; } return n ? s / n : NaN; };

/* ------------------------------ loads and parts: one story for the three kinds ------------------------------ */

const LOADS = [
  { ref: "U3", label: "nRF52840", amps: 0.12, source: "user", cite: "Burst with the radio, the CPU and a sensor board on 3V3, set by you" },
  { ref: "U4", label: "LM75C temperature sensor", amps: 0.0003, source: "datasheet", cite: FIXTURE },
  { ref: "R5", label: "I2C pull-up, SCL", amps: 0.00035, source: "default", cite: "Line low half of the time, 4.7 kΩ to 3.3 V" },
  { ref: "R6", label: "I2C pull-up, SDA", amps: 0.00035, source: "default", cite: "Line low half of the time, 4.7 kΩ to 3.3 V" },
];
const VIN = 5.0, VOUT = 3.3, R_PTC = 0.35;
const totalAmps = (loads) => loads.reduce((a, l) => a + l.amps, 0);

/* ------------------------------ power ------------------------------ */

export function powerRun(board, { id, ref, label, whatif = [], base_run = null, dx = 0.1 }) {
  const g = makeGrid(board.bbox, dx);
  const widen = whatif.find((o) => o.op === "widen_track");
  const widenSpec = widen ? { net: nid(widen.net), width: widen.width_mm } : null;
  const loads = LOADS.map((l) => { const set = whatif.find((o) => o.op === "set_current" && o.ref === l.ref); return set ? { ...l, amps: set.amps, source: "user", cite: "Set in this what-if" } : l; });
  const itot = totalAmps(loads);
  const nets = [
    { net: nid("VBUS_F"), source: padOf(board, "F1", "2"), sinks: [{ pads: [...padOf(board, "U2", "1"), ...padOf(board, "U2", "3")], amps: itot, ref: "U2" }] },
    { net: nid("3V3"), source: padOf(board, "U2", "5"), sinks: loads.map((l) => ({ pads: padOf(board, l.ref).filter((p) => p.net === nid("3V3")), amps: l.amps, ref: l.ref })) },
  ];
  const layers = ["F.Cu", "B.Cu"];
  const all = new Set(nets.map((n) => n.net));
  const masks = layers.map((l) => copperMask(board, g, l, all, widenSpec));
  const sources = layers.map(() => []);
  const sinks = layers.map(() => []);
  const sinkCells = {};
  for (const n of nets) {
    for (const p of n.source) layers.forEach((l, li) => { if (p.layers.includes(l) || p.layers.includes("*.Cu")) sources[li].push(...padCells(board, g, p)); });
    for (const s of n.sinks) {
      const cells = [];
      for (const p of s.pads) layers.forEach((l, li) => { if (p.layers.includes(l) || p.layers.includes("*.Cu")) { const c = padCells(board, g, p); cells.push([li, c]); } });
      const share = s.amps / Math.max(1, cells.length);
      for (const [li, c] of cells) sinks[li].push({ cells: c, amps: share });
      sinkCells[`${n.net}:${s.ref}`] = cells.flatMap(([li, c]) => c.map((k) => [li, k]));
    }
  }
  const vias = board.vias.filter((v) => all.has(v.net)).map((v) => ({ x: v.x, y: v.y, r: v.diameter / 2, layers: [0, 1] }));
  const sol = solveCopper({ g, masks, sources, sinks, vias });
  const mv = sol.drop.map((a) => Float32Array.from(a, (v) => v * 1000));
  const jd = sol.sheet.map((a) => Float32Array.from(a, (v) => v / CU_T_MM));
  const rasters = {};
  const overlays = [];
  layers.forEach((l, li) => {
    const vmaxMv = finiteMax(mv[li]);
    if (!Number.isFinite(vmaxMv)) return;
    overlays.push({ id: `vdrop_${l}`, label: "Voltage drop", unit: "mV", layer: l, ...gridOf(g), vmin: 0, vmax: vmaxMv, file: `vdrop_${l}.bin`, scale: "sequential" });
    rasters[`vdrop_${l}`] = mv[li];
    overlays.push({ id: `jdens_${l}`, label: "Current density", unit: "A/mm²", layer: l, ...gridOf(g), vmin: 0, vmax: pct(jd[li], 0.995), file: `jdens_${l}.bin`, scale: "sequential" });
    rasters[`jdens_${l}`] = jd[li];
  });
  // each load's drop is read on its supply pads, on the 3V3 net (the regulator holds the output, so the drop on this net is what the load loses)
  const loadRows = {};
  let worst = 0, worstRef = "", loss = 0;
  for (const l of loads) {
    const cells = sinkCells[`${nid("3V3")}:${l.ref}`] ?? [];
    if (!cells.length) continue;
    const v = (cells.reduce((a, [li, k]) => a + (Number.isFinite(sol.drop[li][k]) ? sol.drop[li][k] : 0), 0) / cells.length) * 1000;
    loss += (v / 1000) * l.amps;
    if (v >= worst) { worst = v; worstRef = l.ref; }
    loadRows[`load.${l.ref}`] = { value: +v.toFixed(3), unit: "mV", label: l.label, group: "load", ref: l.ref, net: "3V3", current: l.amps, current_unit: "A", limit: 66 };
  }
  const inCells = sinkCells[`${nid("VBUS_F")}:U2`] ?? [];
  const vin = inCells.length ? (inCells.reduce((a, [li, k]) => a + (Number.isFinite(sol.drop[li][k]) ? sol.drop[li][k] : 0), 0) / inCells.length) * 1000 : 0;
  loss += (vin / 1000) * itot;
  const peak = Math.max(...jd.map((a) => finiteMax(a)));
  const metrics = {
    worst_drop: { value: +worst.toFixed(3), unit: "mV", label: `Worst drop, at ${worstRef}` },
    input_drop: { value: +vin.toFixed(3), unit: "mV", label: "Drop to the regulator input (VBUS_F)" },
    supply_current: { value: +(itot * 1000).toFixed(2), unit: "mA", label: "Current from the regulator" },
    peak_current_density: { value: +peak.toFixed(2), unit: "A/mm²", label: "Peak current density" },
    copper_loss: { value: +(loss * 1000).toFixed(4), unit: "mW", label: "Power lost in the copper" },
    ...loadRows,
  };
  const w3 = widenSpec ? widenSpec.width : 0.5;
  const assumptions = [
    ...loads.map((l) => asm(`${l.ref} current`, l.amps, "A", l.source, l.cite)),
    asm("Copper thickness", 35, "µm", "default", "1 oz copper, as no stackup is stored"),
    asm("Sheet resistance", +(RHO_S_1OZ * 1000).toFixed(3), "mΩ/sq", "default", "Copper at 20 °C"),
    asm("Source voltage", VIN, "V", "default", "USB 5 V"),
    asm("Allowed drop", 66, "mV", "default", "2 % of 3.3 V"),
    asm("Track width, 3V3 and VBUS_F", w3, "mm", "board", widenSpec ? "Widened in this what-if" : "Read from the board"),
    asm("Via resistance", 1.5, "mΩ", "default"),
  ];
  const trust = { status: sol.lost === 0 && sol.residual < 1e-6 ? "ok" : "failed", checks: [
    { check: "Solver converged", ok: sol.residual < 1e-6, detail: `Residual ${sol.residual.toExponential(1)} after ${sol.iterations} iterations` },
    { check: "Every load reaches the source", ok: sol.lost === 0, detail: sol.lost === 0 ? "All load pads are connected through copper" : `${(sol.lost * 1000).toFixed(1)} mA is on pads with no copper path` },
    { check: "Strip benchmark", ok: true, detail: "The same solver matches I x R of a straight strip within 1 % (checked in the tests)" },
  ] };
  const result = { kind: "power", status: "ok", reason: null, metrics, trust, overlays, assumptions, notes: ["The drop is measured on the copper between the source pad and each load pad. Vias are modelled with a fixed resistance."], solve_s: 1.4 };
  return { row: runRow({ id, ref, kind: "power", label, whatif, base_run, result }), rasters, input: { itot, vin } };
}

/* ------------------------------ thermal ------------------------------ */

const PARTS = (itot, vin) => [
  { ref: "U2", label: "AP2112K regulator", w: (VIN - VOUT - vin / 1000) * itot, theta: 250, theta_source: "datasheet", theta_cite: FIXTURE },
  { ref: "U3", label: "nRF52840", w: VOUT * LOADS[0].amps, theta: 18, theta_source: "default", theta_cite: "Small package assumed" },
  { ref: "F1", label: "Resettable fuse", w: itot * itot * R_PTC, theta: 90, theta_source: "default" },
];

export function thermalRun(board, { id, ref, label, whatif = [], base_run = null, dx = 0.25, itot, vin }) {
  const g = makeGrid(board.bbox, dx);
  const outline = new Uint8Array(g.nx * g.ny);
  fillRings(outline, g, board.outline);
  const allNets = new Set([...board.tracks, ...board.pads, ...board.vias, ...board.zones].map((x) => x.net).filter(Boolean));
  const cov = ["F.Cu", "B.Cu"].map((l) => boxBlur(Float32Array.from(copperMask(board, g, l, allNets)), g.nx, g.ny, Math.round(1.2 / dx)));
  const K_FR4 = 0.3e-3, T_FR4 = 1.6, K_CU = 0.385, H = 26e-6;
  const gSheet = new Float32Array(g.nx * g.ny).fill(NaN);
  let covSum = 0, covN = 0;
  for (let k = 0; k < gSheet.length; k++) if (outline[k]) { const c = (cov[0][k] + cov[1][k]) / 2; gSheet[k] = K_FR4 * T_FR4 + K_CU * CU_T_MM * (cov[0][k] + cov[1][k]); covSum += c; covN++; }
  const parts = PARTS(itot, vin).map((p) => { const set = whatif.find((o) => o.op === "set_part_power" && o.ref === p.ref); return set ? { ...p, w: set.watts, edited: true } : p; });
  const power = new Float64Array(g.nx * g.ny);
  for (const p of parts) {
    const fp = board.footprints.find((f) => f.ref === p.ref);
    if (!fp) continue;
    const cells = [];
    for (let j = Math.floor((fp.bbox[1] - g.y0) / dx); j <= Math.floor((fp.bbox[3] - g.y0) / dx); j++) for (let i = Math.floor((fp.bbox[0] - g.x0) / dx); i <= Math.floor((fp.bbox[2] - g.x0) / dx); i++) if (i >= 0 && j >= 0 && i < g.nx && j < g.ny && outline[j * g.nx + i]) cells.push(j * g.nx + i);
    p.cells = cells;
    for (const k of cells) power[k] += p.w / cells.length;
  }
  const sol = solvePlate({ g, gSheet, h: H, power });
  const AMBIENT = 25;
  const field = Float32Array.from(sol.rise, (v) => v + AMBIENT);
  const lo = finiteMin(field), hi = finiteMax(field);
  const rows = {};
  let hottest = { ref: "", t: -Infinity };
  for (const p of parts.slice().sort((a, b) => b.w - a.w)) {
    if (!p.cells?.length) continue;
    const board_t = meanAt(field, p.cells);
    const t = board_t + p.w * p.theta;
    if (t > hottest.t) hottest = { ref: p.ref, t };
    rows[`part.${p.ref}`] = { value: +t.toFixed(2), unit: "°C", label: p.label, group: "part", ref: p.ref, power: +p.w.toFixed(4), power_unit: "W", board: +board_t.toFixed(2), limit: 85 };
  }
  const overlays = [{ id: "temp", label: "Temperature", unit: "°C", layer: "board", ...gridOf(g), vmin: lo, vmax: hi, file: "temp.bin", scale: "sequential" }];
  const totalW = parts.reduce((a, p) => a + p.w, 0);
  const metrics = {
    hottest_part: { value: +hottest.t.toFixed(2), unit: "°C", label: `Hottest part, ${hottest.ref}` },
    board_peak: { value: +hi.toFixed(2), unit: "°C", label: "Hottest point of the board" },
    board_rise: { value: +(hi - AMBIENT).toFixed(2), unit: "K", label: "Board rise above the air" },
    total_power: { value: +(totalW * 1000).toFixed(1), unit: "mW", label: "Heat put into the board" },
    ...rows,
  };
  const assumptions = [
    ...parts.filter((p) => p.cells?.length).map((p) => asm(`${p.ref} dissipation`, +p.w.toFixed(4), "W", p.edited ? "user" : "default", p.edited ? "Set in this what-if" : "From the current and voltage drop of this review")),
    ...parts.filter((p) => p.cells?.length).map((p) => asm(`${p.ref} thermal resistance, part to board`, p.theta, "K/W", p.theta_source, p.theta_cite)),
    asm("Air temperature", AMBIENT, "°C", "default"),
    asm("Heat loss to air, both faces", 26, "W/m²K", "default", "Still air, convection and radiation"),
    asm("Board material", "FR-4, 1.6 mm", "", "default", "0.3 W/mK"),
    asm("Copper share, both layers", +((covSum / covN) * 100).toFixed(0), "%", "board", "Measured from the copper of the board"),
  ];
  const trust = { status: sol.residual < 1e-6 ? "ok" : "failed", checks: [
    { check: "Solver converged", ok: sol.residual < 1e-6, detail: `Residual ${sol.residual.toExponential(1)} after ${sol.iterations} iterations` },
    { check: "Heat balance", ok: true, detail: "Heat lost to the air equals the heat put in, within 1 % (checked in the tests)" },
  ] };
  const result = { kind: "thermal", status: "ok", reason: null, metrics, trust, overlays, assumptions, notes: ["The board is one plate: heat spreads sideways through the copper and the laminate, and leaves through both faces. Part temperatures add the part-to-board resistance."], solve_s: 2.1 };
  return { row: runRow({ id, ref, kind: "thermal", label, whatif, base_run, result }), rasters: { temp: field } };
}

/* ------------------------------ signal ------------------------------ */

/** The track chain of one net from a pad: an ordered list of {width, pts} along the longest path. */
function chainOf(board, net, layer, from) {
  const tracks = board.tracks.filter((t) => t.net === net && t.layer === layer).map((t) => ({ t, pts: t.mid ? arcPoints(t.start, t.mid, t.end) : [t.start, t.end] }));
  const key = (p) => `${Math.round(p[0] * 100)},${Math.round(p[1] * 100)}`;
  const at = new Map();
  tracks.forEach((s, i) => { for (const p of [s.pts[0], s.pts[s.pts.length - 1]]) { const k = key(p); if (!at.has(k)) at.set(k, []); at.get(k).push(i); } });
  let best = null, bestD = Infinity;
  for (const s of tracks) for (const p of [s.pts[0], s.pts[s.pts.length - 1]]) { const d = Math.hypot(p[0] - from[0], p[1] - from[1]); if (d < bestD) { bestD = d; best = p; } }
  let longest = [];
  const walk = (k, used, path) => {
    let moved = false;
    for (const i of at.get(k) ?? []) {
      if (used.has(i)) continue;
      const s = tracks[i];
      const fwd = key(s.pts[0]) === k;
      const pts = fwd ? s.pts : s.pts.slice().reverse();
      moved = true;
      walk(key(pts[pts.length - 1]), new Set([...used, i]), [...path, { width: s.t.width, pts }]);
    }
    if (!moved && path.length > longest.length) longest = path;
  };
  if (best) walk(key(best), new Set(), []);
  return longest;
}
const lenOf = (pts) => pts.reduce((a, p, i) => (i ? a + Math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) : 0), 0);

export function signalRun(board, { id, ref, label, whatif = [], base_run = null, dx = 0.1 }) {
  const NET = "ANT_FEED", LAYER = "F.Cu", H = 0.2, ER = 4.3, TARGET = 50, TOL = 10;
  const widen = whatif.find((o) => o.op === "widen_track");
  const g = makeGrid(board.bbox, dx);
  const start = padOf(board, "L1", "2")[0];
  const chain = chainOf(board, nid(NET), LAYER, [start.x, start.y]);
  const widthOf = (w) => (widen && w < widen.width_mm ? widen.width_mm : w);
  // impedance of every cell of the net's copper: the track it belongs to
  const zdev = new Float32Array(g.nx * g.ny).fill(NaN);
  const detour = new Float32Array(g.nx * g.ny).fill(NaN);
  const ref0 = new Uint8Array(g.nx * g.ny);
  for (const z of board.zones) if (z.net === nid("GND") && z.filled["B.Cu"]) fillRings(ref0, g, z.filled["B.Cu"]);
  const dist = distanceTo(ref0, g.nx, g.ny, dx);
  const sections = [], profX = [], profZ = [];
  let run = 0;
  const segs = [];
  for (const s of chain) {
    const w = widthOf(s.width), ms = microstrip(w, H, ER), len = lenOf(s.pts);
    const dev = ((ms.z - TARGET) / TARGET) * 100;
    const buf = new Uint8Array(g.nx * g.ny);
    const pts = s.pts;
    for (let k = 0; k + 1 < pts.length; k++) {
      const [ax, ay] = pts[k], [bx, by] = pts[k + 1];
      const i0 = Math.max(0, Math.floor((Math.min(ax, bx) - w / 2 - g.x0) / dx)), i1 = Math.min(g.nx - 1, Math.floor((Math.max(ax, bx) + w / 2 - g.x0) / dx));
      const j0 = Math.max(0, Math.floor((Math.min(ay, by) - w / 2 - g.y0) / dx)), j1 = Math.min(g.ny - 1, Math.floor((Math.max(ay, by) + w / 2 - g.y0) / dx));
      const vx = bx - ax, vy = by - ay, l2 = vx * vx + vy * vy;
      for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) {
        const px = g.x0 + (i + 0.5) * dx, py = g.y0 + (j + 0.5) * dx;
        const t = l2 ? Math.max(0, Math.min(1, ((px - ax) * vx + (py - ay) * vy) / l2)) : 0;
        if (Math.hypot(px - (ax + vx * t), py - (ay + vy * t)) <= w / 2) { buf[j * g.nx + i] = 1; zdev[j * g.nx + i] = dev; detour[j * g.nx + i] = dist[j * g.nx + i]; }
      }
    }
    const prev = segs[segs.length - 1];
    if (prev && Math.abs(prev.w - w) < 1e-6) { prev.len += len; prev.sec.delay_ps += len * ms.delayPsPerMm; }
    else { const sec = { z: ms.z, delay_ps: len * ms.delayPsPerMm }; segs.push({ w, z: ms.z, len, dev, sec }); sections.push(sec); }
    for (let d = 0; d < len; d += 0.05) { profX.push(+(run + d).toFixed(3)); profZ.push(+ms.z.toFixed(3)); }
    run += len;
  }
  const lo = Math.min(...profZ), hi = Math.max(...profZ);
  const worstZ = Math.abs(lo - TARGET) > Math.abs(hi - TARGET) ? lo : hi;
  const worstDev = ((worstZ - TARGET) / TARGET) * 100;
  const lim = Math.max(10, Math.ceil(Math.max(...zdev.filter(Number.isFinite).map(Math.abs)) / 10) * 10);
  const t = tdr(sections, { zSource: TARGET, zLoad: TARGET });
  const tdrN = t.t_ps.filter((_, i) => i % 2 === 0);
  const maxDetour = finiteMax(detour);
  const metrics = {
    worst_deviation: { value: +worstDev.toFixed(1), unit: "%", label: "Worst impedance deviation" },
    z_min: { value: +lo.toFixed(1), unit: "Ω", label: "Lowest impedance" },
    z_max: { value: +hi.toFixed(1), unit: "Ω", label: "Highest impedance" },
    length: { value: +run.toFixed(2), unit: "mm", label: "Length of the net" },
    delay: { value: +sections.reduce((a, s) => a + s.delay_ps, 0).toFixed(1), unit: "ps", label: "One-way delay" },
    detour_max: { value: Number.isFinite(maxDetour) ? +maxDetour.toFixed(2) : 0, unit: "mm", label: "Largest return detour" },
    ...Object.fromEntries(segs.map((s, i) => [`segment.${i + 1}`, { value: +s.z.toFixed(1), unit: "Ω", label: `${s.w.toFixed(2)} mm wide, ${s.len.toFixed(1)} mm long`, group: "segment", deviation: +s.dev.toFixed(1), limit: TOL }])),
  };
  const overlays = [
    { id: "zdev", label: "Impedance deviation", unit: "%", layer: LAYER, ...gridOf(g), vmin: -lim, vmax: lim, file: "zdev.bin", scale: "diverging" },
    { id: "detour", label: "Return detour", unit: "mm", layer: LAYER, ...gridOf(g), vmin: 0, vmax: Math.max(0.5, Math.ceil(maxDetour * 2) / 2 || 0.5), file: "detour.bin", scale: "sequential" },
  ];
  const assumptions = [
    asm("Net", NET, "", "user", "The net this run looks at"),
    asm("Target impedance", TARGET, "Ω", "user"),
    asm("Tolerance", TOL, "%", "default"),
    asm("Height above the reference plane", widen ? H : H, "mm", "user", "Dielectric between the track and the plane"),
    asm("Relative permittivity", ER, "", "default", "FR-4"),
    asm("Copper thickness", 35, "µm", "default"),
    asm("Track width", segs.map((s) => s.w.toFixed(2)).join(", "), "mm", "board", widen ? `Narrow tracks widened to ${widen.width_mm} mm in this what-if` : "Read from the board"),
    asm("Source and load", TARGET, "Ω", "default", "Both ends matched"),
    asm("Edge rise time", 30, "ps", "default", "10 to 90 %"),
  ];
  const trust = { status: "ok", checks: [
    { check: "Straight line benchmark", ok: true, detail: "A uniform line shows a flat trace at its own impedance (checked in the tests)" },
    { check: "Single step benchmark", ok: true, detail: "A step from 50 to 70 Ω settles at 70 Ω (checked in the tests)" },
  ] };
  const series = [
    { id: "profile", label: "Impedance along the net", x_label: "Distance along the net", x_unit: "mm", y_unit: "Ω", x: profX, y: profZ, target: TARGET, tolerance_pct: TOL },
    { id: "tdr", label: "Time domain reflection", x_label: "Time", x_unit: "ns", y_unit: "Ω", x: tdrN.map((v) => +(v / 1000).toFixed(4)), y: t.z.filter((_, i) => i % 2 === 0).map((v) => +v.toFixed(2)), target: TARGET, tolerance_pct: TOL },
  ];
  const result = { kind: "signal", status: "ok", reason: null, metrics, trust, overlays, assumptions, notes: ["Return detour is the distance from each track cell to the nearest copper of the reference plane below. The reflection curve counts each interface once."], series, solve_s: 0.8 };
  return { row: runRow({ id, ref, kind: "signal", label, whatif, base_run, result }), rasters: { zdev, detour } };
}

/* ------------------------------ the set ------------------------------ */

function runRow({ id, ref, kind, label, whatif, base_run, result, status = "done" }) {
  return { run_id: id, kind, status, label, ref, whatif, base_run, progress: status === "done" ? 1 : 0, step: null, result, evidence: null, created_at: "2026-10-31T16:00:00Z", fixture: true };
}

/**
 * The runs of the dev review: a power run and a what-if that widens the 3V3 tracks, a thermal run and a what-if that cuts the regulator's heat,
 * a signal run and a what-if that widens the feed, plus one run being solved and one waiting for the solver.
 */
export function buildFixtureRuns(board, { fine = true } = {}) {
  const dx = fine ? 0.1 : 0.2;
  const p1 = powerRun(board, { id: "fx-p1", ref: "R1", label: "Supply path as drawn", dx });
  const p2 = powerRun(board, { id: "fx-p2", ref: "R2", label: "Widen 3V3 to 0.9 mm", whatif: [{ op: "widen_track", net: "3V3", width_mm: 0.9 }], base_run: "fx-p1", dx });
  const { itot, vin } = p1.input;
  const t1 = thermalRun(board, { id: "fx-t1", ref: "R3", label: "Board heat as drawn", itot, vin });
  const t2 = thermalRun(board, { id: "fx-t2", ref: "R4", label: "Regulator dissipates 40 mW", whatif: [{ op: "set_part_power", ref: "U2", watts: 0.04 }], base_run: "fx-t1", itot, vin });
  const s1 = signalRun(board, { id: "fx-s1", ref: "R5", label: "Antenna feed as drawn", dx });
  const s2 = signalRun(board, { id: "fx-s2", ref: "R6", label: "Widen the feed to 0.34 mm", whatif: [{ op: "widen_track", net: "ANT_FEED", width_mm: 0.34 }], base_run: "fx-s1", dx });
  const runs = [p1, p2, t1, t2, s1, s2].map((r) => r.row);
  runs.push({ ...runRow({ id: "fx-t3", ref: "R7", kind: "thermal", label: "Regulator on the bottom side", whatif: [{ op: "set_part_power", ref: "U2", watts: 0.08 }], base_run: "fx-t1", result: null, status: "running" }), progress: 0.42, step: "Solving the plate" });
  runs.push({ ...runRow({ id: "fx-p3", ref: "R8", kind: "power", label: "Raise the radio current to 80 mA", whatif: [{ op: "set_current", ref: "U3", amps: 0.08 }], base_run: "fx-p1", result: null, status: "queued" }), progress: 0, step: "waiting", waiting: { position: 2, ahead: 2 } });
  const rasters = new Map();
  for (const r of [p1, p2, t1, t2, s1, s2]) for (const [k, v] of Object.entries(r.rasters)) rasters.set(`${r.row.run_id}/${k}`, v);
  return { runs, rasters };
}
