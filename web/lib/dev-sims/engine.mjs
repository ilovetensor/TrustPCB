// Small physics for the dev fixtures: a resistive copper network, a heat spreading plate and transmission line formulas.
// These run only in development, to give the simulation lab realistic input before the real simulators are connected.
// They are checked against closed-form answers in tests/dev-sims.test.mjs.

export const RHO_S_1OZ = 0.49e-3;       // ohm per square, 35 um copper
export const CU_T_MM = 0.035;

/* ------------------------------ raster ------------------------------ */

export function makeGrid(bbox, dx, margin = 0) {
  const x0 = bbox[0] - margin, y0 = bbox[1] - margin;
  return { x0, y0, dx, nx: Math.ceil((bbox[2] - bbox[0] + 2 * margin) / dx), ny: Math.ceil((bbox[3] - bbox[1] + 2 * margin) / dx) };
}

export function fillCapsule(buf, g, ax, ay, bx, by, w, val = 1) {
  const r = w / 2;
  const i0 = Math.max(0, Math.floor((Math.min(ax, bx) - r - g.x0) / g.dx)), i1 = Math.min(g.nx - 1, Math.floor((Math.max(ax, bx) + r - g.x0) / g.dx));
  const j0 = Math.max(0, Math.floor((Math.min(ay, by) - r - g.y0) / g.dx)), j1 = Math.min(g.ny - 1, Math.floor((Math.max(ay, by) + r - g.y0) / g.dx));
  const vx = bx - ax, vy = by - ay, l2 = vx * vx + vy * vy;
  for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) {
    const px = g.x0 + (i + 0.5) * g.dx, py = g.y0 + (j + 0.5) * g.dx;
    const k = l2 > 0 ? Math.max(0, Math.min(1, ((px - ax) * vx + (py - ay) * vy) / l2)) : 0;
    if (Math.hypot(px - (ax + vx * k), py - (ay + vy * k)) <= r) buf[j * g.nx + i] = val;
  }
}

export const fillCircle = (buf, g, cx, cy, r, val = 1) => fillCapsule(buf, g, cx, cy, cx, cy, 2 * r, val);

/** Fills the inside of rings (even-odd over all rings together, so a ring inside another is a hole). */
export function fillRings(buf, g, rings, val = 1) {
  for (let j = 0; j < g.ny; j++) {
    const y = g.y0 + (j + 0.5) * g.dx;
    const xs = [];
    for (const ring of rings) {
      for (let a = 0, n = ring.length; a < n; a++) {
        const p = ring[a], q = ring[(a + 1) % n];
        if ((p[1] <= y) !== (q[1] <= y)) xs.push(p[0] + ((y - p[1]) / (q[1] - p[1])) * (q[0] - p[0]));
      }
    }
    xs.sort((a, b) => a - b);
    for (let k = 0; k + 1 < xs.length; k += 2) {
      const i0 = Math.max(0, Math.ceil((xs[k] - g.x0) / g.dx - 0.5)), i1 = Math.min(g.nx - 1, Math.floor((xs[k + 1] - g.x0) / g.dx - 0.5));
      for (let i = i0; i <= i1; i++) buf[j * g.nx + i] = val;
    }
  }
}

/** Points along the circular arc through start, mid and end. */
export function arcPoints(a, m, b, n = 14) {
  const d = 2 * (a[0] * (m[1] - b[1]) + m[0] * (b[1] - a[1]) + b[0] * (a[1] - m[1]));
  if (Math.abs(d) < 1e-9) return [a, b];
  const ux = ((a[0] ** 2 + a[1] ** 2) * (m[1] - b[1]) + (m[0] ** 2 + m[1] ** 2) * (b[1] - a[1]) + (b[0] ** 2 + b[1] ** 2) * (a[1] - m[1])) / d;
  const uy = ((a[0] ** 2 + a[1] ** 2) * (b[0] - m[0]) + (m[0] ** 2 + m[1] ** 2) * (a[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (m[0] - a[0])) / d;
  const r = Math.hypot(a[0] - ux, a[1] - uy);
  const ang = (p) => Math.atan2(p[1] - uy, p[0] - ux);
  const a0 = ang(a), am = ang(m), a1 = ang(b);
  const norm = (x) => { while (x < 0) x += 2 * Math.PI; while (x >= 2 * Math.PI) x -= 2 * Math.PI; return x; };
  let sweep = norm(a1 - a0);
  if (norm(am - a0) > sweep) sweep -= 2 * Math.PI;
  const out = [];
  for (let k = 0; k <= n; k++) { const t = a0 + (sweep * k) / n; out.push([ux + r * Math.cos(t), uy + r * Math.sin(t)]); }
  return out;
}

const hasLayer = (layers, name) => layers.includes(name) || layers.includes("*.Cu");

/** Copper of the given nets on one layer: tracks, pads, vias and the zone fill. `widen` is {net, width} for a what-if. */
export function copperMask(board, g, layer, nets, widen = null) {
  const buf = new Uint8Array(g.nx * g.ny);
  const want = (n) => n != null && nets.has(n);
  for (const z of board.zones) if (want(z.net) && z.filled[layer]) fillRings(buf, g, z.filled[layer]);
  for (const t of board.tracks) {
    if (t.layer !== layer || !want(t.net)) continue;
    const w = widen && widen.net === t.net ? Math.max(t.width, widen.width) : t.width;
    const pts = t.mid ? arcPoints(t.start, t.mid, t.end) : [t.start, t.end];
    for (let k = 0; k + 1 < pts.length; k++) fillCapsule(buf, g, pts[k][0], pts[k][1], pts[k + 1][0], pts[k + 1][1], w);
  }
  for (const p of board.pads) if (want(p.net) && hasLayer(p.layers, layer)) fillRings(buf, g, [p.ring]);
  for (const v of board.vias) if (want(v.net) && v.layers.some((l) => l === layer || l === "*.Cu" || l === "F.Cu" || l === "B.Cu")) fillCircle(buf, g, v.x, v.y, v.diameter / 2);
  return buf;
}

/* ------------------------------ linear solver ------------------------------ */

/** Conjugate gradients with a Jacobi preconditioner for a symmetric positive definite operator. */
export function cg(n, apply, b, diag, { tol = 1e-10, maxIter = 4000 } = {}) {
  const x = new Float64Array(n), r = Float64Array.from(b), z = new Float64Array(n), p = new Float64Array(n), q = new Float64Array(n);
  let bn = 0;
  for (let i = 0; i < n; i++) bn += b[i] * b[i];
  if (bn === 0) return { x, iterations: 0, residual: 0 };
  for (let i = 0; i < n; i++) { z[i] = r[i] / diag[i]; p[i] = z[i]; }
  let rz = 0;
  for (let i = 0; i < n; i++) rz += r[i] * z[i];
  let it = 0, rn = Math.sqrt(bn);
  for (; it < maxIter; it++) {
    apply(p, q);
    let pq = 0;
    for (let i = 0; i < n; i++) pq += p[i] * q[i];
    const alpha = rz / pq;
    rn = 0;
    for (let i = 0; i < n; i++) { x[i] += alpha * p[i]; r[i] -= alpha * q[i]; rn += r[i] * r[i]; }
    rn = Math.sqrt(rn);
    if (rn <= tol * Math.sqrt(bn)) break;
    let rz2 = 0;
    for (let i = 0; i < n; i++) { z[i] = r[i] / diag[i]; rz2 += r[i] * z[i]; }
    const beta = rz2 / rz;
    rz = rz2;
    for (let i = 0; i < n; i++) p[i] = z[i] + beta * p[i];
  }
  return { x, iterations: it, residual: rn / Math.sqrt(bn) };
}

/* ------------------------------ copper network: voltage drop and current density ------------------------------ */

/**
 * Solves the drop across copper layers. `masks` is one mask per layer (same grid). `sources` and `sinks` are lists of cell lists
 * per layer: sinks carry amps. Returns, per layer, the drop in volts and the sheet current density in A/mm (NaN where there is no copper
 * that connects to a source). `vias` is [{x, y, r, layers: [a, b]}] with the resistance of one via in ohm.
 */
export function solveCopper({ g, masks, rhoS = RHO_S_1OZ, sources, sinks, vias = [], viaOhm = 0.0015 }) {
  const L = masks.length;
  const idx = masks.map((m) => { const a = new Int32Array(m.length).fill(-1); return a; });
  let n = 0;
  const cellOf = [], layerOf = [];
  for (let l = 0; l < L; l++) for (let k = 0; k < masks[l].length; k++) if (masks[l][k]) { idx[l][k] = n++; cellOf.push(k); layerOf.push(l); }
  const gs = 1 / rhoS;
  const nbr = new Int32Array(n * 4).fill(-1);
  const via = new Int32Array(n).fill(-1);
  const gv = new Float64Array(n);
  for (let l = 0; l < L; l++) for (let j = 0; j < g.ny; j++) for (let i = 0; i < g.nx; i++) {
    const a = idx[l][j * g.nx + i];
    if (a < 0) continue;
    if (i > 0) nbr[a * 4] = idx[l][j * g.nx + i - 1];
    if (i < g.nx - 1) nbr[a * 4 + 1] = idx[l][j * g.nx + i + 1];
    if (j > 0) nbr[a * 4 + 2] = idx[l][(j - 1) * g.nx + i];
    if (j < g.ny - 1) nbr[a * 4 + 3] = idx[l][(j + 1) * g.nx + i];
  }
  for (const v of vias) {
    const cells = [];
    const r = v.r;
    for (let j = Math.floor((v.y - r - g.y0) / g.dx); j <= Math.floor((v.y + r - g.y0) / g.dx); j++) for (let i = Math.floor((v.x - r - g.x0) / g.dx); i <= Math.floor((v.x + r - g.x0) / g.dx); i++) {
      if (i < 0 || j < 0 || i >= g.nx || j >= g.ny) continue;
      if (Math.hypot(g.x0 + (i + 0.5) * g.dx - v.x, g.y0 + (j + 0.5) * g.dx - v.y) > r) continue;
      const a = idx[v.layers[0]][j * g.nx + i], b = idx[v.layers[1]][j * g.nx + i];
      if (a >= 0 && b >= 0) cells.push([a, b]);
    }
    for (const [a, b] of cells) { via[a] = b; via[b] = a; gv[a] = gv[b] = cells.length / viaOhm; }
  }
  // what the sources reach
  const fixed = new Uint8Array(n), reach = new Uint8Array(n);
  const queue = [];
  sources.forEach((cells, l) => { for (const k of cells) { const a = idx[l][k]; if (a >= 0 && !reach[a]) { reach[a] = 1; fixed[a] = 1; queue.push(a); } } });
  for (let h = 0; h < queue.length; h++) {
    const a = queue[h];
    for (let s = 0; s < 4; s++) { const b = nbr[a * 4 + s]; if (b >= 0 && !reach[b]) { reach[b] = 1; queue.push(b); } }
    if (via[a] >= 0 && !reach[via[a]]) { reach[via[a]] = 1; queue.push(via[a]); }
  }
  const rhs = new Float64Array(n);
  let injected = 0, lost = 0;
  sinks.forEach((list, l) => { for (const { cells, amps } of list) {
    const live = cells.map((k) => idx[l][k]).filter((a) => a >= 0 && reach[a] && !fixed[a]);
    if (!live.length) { lost += amps; continue; }
    for (const a of live) rhs[a] += amps / live.length;
    injected += amps;
  } });
  const diag = new Float64Array(n);
  for (let a = 0; a < n; a++) {
    if (!reach[a] || fixed[a]) { diag[a] = 1; continue; }
    let d = 0;
    for (let s = 0; s < 4; s++) if (nbr[a * 4 + s] >= 0) d += gs;
    if (via[a] >= 0) d += gv[a];
    diag[a] = d;
  }
  const apply = (x, y) => {
    for (let a = 0; a < n; a++) {
      if (!reach[a] || fixed[a]) { y[a] = x[a]; continue; }
      let s = diag[a] * x[a];
      for (let t = 0; t < 4; t++) { const b = nbr[a * 4 + t]; if (b >= 0 && reach[b] && !fixed[b]) s -= gs * x[b]; }
      if (via[a] >= 0 && reach[via[a]] && !fixed[via[a]]) s -= gv[a] * x[via[a]];
      y[a] = s;
    }
  };
  const sol = cg(n, apply, rhs, diag);
  const volts = sol.x;
  const drop = masks.map(() => new Float32Array(g.nx * g.ny).fill(NaN));
  const sheet = masks.map(() => new Float32Array(g.nx * g.ny).fill(NaN));
  for (let a = 0; a < n; a++) {
    if (!reach[a]) continue;
    const l = layerOf[a], k = cellOf[a];
    drop[l][k] = volts[a];
    let kx = 0, kxn = 0, ky = 0, kyn = 0;
    const lt = nbr[a * 4], rt = nbr[a * 4 + 1], up = nbr[a * 4 + 2], dn = nbr[a * 4 + 3];
    if (lt >= 0 && reach[lt]) { kx += gs * (volts[lt] - volts[a]); kxn++; }
    if (rt >= 0 && reach[rt]) { kx += gs * (volts[a] - volts[rt]); kxn++; }
    if (up >= 0 && reach[up]) { ky += gs * (volts[up] - volts[a]); kyn++; }
    if (dn >= 0 && reach[dn]) { ky += gs * (volts[a] - volts[dn]); kyn++; }
    sheet[l][k] = Math.hypot(kxn ? kx / kxn : 0, kyn ? ky / kyn : 0) / g.dx;
  }
  return { drop, sheet, iterations: sol.iterations, residual: sol.residual, injected, lost, nodes: n };
}

/* ------------------------------ heat spreading ------------------------------ */

/**
 * Steady temperature rise of a thin plate with a heat loss to the air over its whole area: (div(G grad T) - h T + q = 0).
 * `gSheet` is the sheet conductance in W/K per cell (Float32Array, NaN outside the board), `h` is W per mm2 per K, `power` is W per cell.
 */
export function solvePlate({ g, gSheet, h, power }) {
  const n = g.nx * g.ny;
  const ok = (k) => Number.isFinite(gSheet[k]);
  const nb = (k, s) => {
    const i = k % g.nx, j = (k / g.nx) | 0;
    const m = s === 0 ? (i > 0 ? k - 1 : -1) : s === 1 ? (i < g.nx - 1 ? k + 1 : -1) : s === 2 ? (j > 0 ? k - g.nx : -1) : (j < g.ny - 1 ? k + g.nx : -1);
    return m >= 0 && ok(m) ? m : -1;
  };
  const edge = (a, b) => (2 * gSheet[a] * gSheet[b]) / (gSheet[a] + gSheet[b]);
  const diag = new Float64Array(n).fill(1);
  const b = new Float64Array(n);
  const cellLoss = h * g.dx * g.dx;
  for (let k = 0; k < n; k++) {
    if (!ok(k)) continue;
    let d = cellLoss;
    for (let s = 0; s < 4; s++) { const m = nb(k, s); if (m >= 0) d += edge(k, m); }
    diag[k] = d; b[k] = power[k];
  }
  const apply = (x, y) => {
    for (let k = 0; k < n; k++) {
      if (!ok(k)) { y[k] = x[k]; continue; }
      let s = diag[k] * x[k];
      for (let t = 0; t < 4; t++) { const m = nb(k, t); if (m >= 0) s -= edge(k, m) * x[m]; }
      y[k] = s;
    }
  };
  const sol = cg(n, apply, b, diag, { tol: 1e-9 });
  const out = new Float32Array(n).fill(NaN);
  for (let k = 0; k < n; k++) if (ok(k)) out[k] = sol.x[k];
  return { rise: out, iterations: sol.iterations, residual: sol.residual };
}

/** Mean of a box blur of radius `r` cells, used for the local copper share. */
export function boxBlur(src, nx, ny, r) {
  const tmp = new Float32Array(src.length), out = new Float32Array(src.length);
  for (let j = 0; j < ny; j++) {
    let s = 0;
    for (let i = -r; i <= r; i++) s += src[j * nx + Math.min(nx - 1, Math.max(0, i))];
    for (let i = 0; i < nx; i++) {
      tmp[j * nx + i] = s / (2 * r + 1);
      s += src[j * nx + Math.min(nx - 1, i + r + 1)] - src[j * nx + Math.max(0, i - r)];
    }
  }
  for (let i = 0; i < nx; i++) {
    let s = 0;
    for (let j = -r; j <= r; j++) s += tmp[Math.min(ny - 1, Math.max(0, j)) * nx + i];
    for (let j = 0; j < ny; j++) {
      out[j * nx + i] = s / (2 * r + 1);
      s += tmp[Math.min(ny - 1, j + r + 1) * nx + i] - tmp[Math.max(0, j - r) * nx + i];
    }
  }
  return out;
}

/* ------------------------------ transmission lines ------------------------------ */

const C_MM_PER_PS = 0.299792458;

/** Characteristic impedance and effective permittivity of a microstrip (Hammerstad and Jensen), width w over a dielectric of height h. */
export function microstrip(w, h, er, t = CU_T_MM) {
  const u0 = w / h;
  const dw = u0 >= 1 / (2 * Math.PI) ? (t / Math.PI) * (1 + Math.log((2 * h) / t)) : (t / Math.PI) * (1 + Math.log((4 * Math.PI * w) / t));
  const u = (w + dw) / h;
  const eEff = (er + 1) / 2 + ((er - 1) / 2) * Math.pow(1 + 12 / u, -0.5);
  const z = u <= 1 ? (60 / Math.sqrt(eEff)) * Math.log(8 / u + u / 4) : (120 * Math.PI) / (Math.sqrt(eEff) * (u + 1.393 + 0.667 * Math.log(u + 1.444)));
  return { z, eEff, delayPsPerMm: Math.sqrt(eEff) / C_MM_PER_PS };
}

/**
 * Time domain reflection seen at the source from a chain of line sections [{z, delay_ps}] ending in a load, for a step of a given rise time.
 * First order: each interface reflects its share once, the reflections are not reflected again. Returns {t_ps, z}.
 */
export function tdr(sections, { zSource = 50, zLoad = 50, risePs = 30, stepPs = 2, endPs = null } = {}) {
  const zs = sections.map((s) => s.z);
  const edges = [];
  let tAcc = 0, through = 1;
  for (let i = 0; i <= zs.length; i++) {
    const zl = i === 0 ? zSource : zs[i - 1], zr = i === zs.length ? zLoad : zs[i];
    const gam = (zr - zl) / (zr + zl);
    if (i > 0) tAcc += sections[i - 1].delay_ps;
    edges.push({ at: 2 * tAcc, gam: through * gam });
    through *= 1 - gam * gam;
  }
  const total = 2 * sections.reduce((a, s) => a + s.delay_ps, 0);
  const end = endPs ?? total + 6 * risePs;
  const t = [], z = [];
  const sigma = risePs / 2.563;                    // 10 to 90 percent rise time of a gaussian edge
  const erf = (x) => { const s = Math.sign(x), a = Math.abs(x), k = 1 / (1 + 0.3275911 * a); return s * (1 - (((((1.061405429 * k - 1.453152027) * k) + 1.421413741) * k - 0.284496736) * k + 0.254829592) * k * Math.exp(-a * a)); };
  for (let ts = 0; ts <= end + 1e-9; ts += stepPs) {
    let rho = 0;
    for (const e of edges) rho += e.gam * 0.5 * (1 + erf((ts - e.at) / (sigma * Math.SQRT2)));
    rho = Math.max(-0.98, Math.min(0.98, rho));
    t.push(ts); z.push(zSource * (1 + rho) / (1 - rho));
  }
  return { t_ps: t, z };
}

/** A distance map in mm from every cell to the nearest cell where `mask` is set (two-pass chamfer, good to a few percent). */
export function distanceTo(mask, nx, ny, dx) {
  const INF = 1e9, d = new Float32Array(nx * ny);
  for (let k = 0; k < d.length; k++) d[k] = mask[k] ? 0 : INF;
  const a = dx, b = dx * Math.SQRT2;
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const k = j * nx + i; let v = d[k];
    if (i > 0) v = Math.min(v, d[k - 1] + a);
    if (j > 0) { v = Math.min(v, d[k - nx] + a); if (i > 0) v = Math.min(v, d[k - nx - 1] + b); if (i < nx - 1) v = Math.min(v, d[k - nx + 1] + b); }
    d[k] = v;
  }
  for (let j = ny - 1; j >= 0; j--) for (let i = nx - 1; i >= 0; i--) {
    const k = j * nx + i; let v = d[k];
    if (i < nx - 1) v = Math.min(v, d[k + 1] + a);
    if (j < ny - 1) { v = Math.min(v, d[k + nx] + a); if (i < nx - 1) v = Math.min(v, d[k + nx + 1] + b); if (i > 0) v = Math.min(v, d[k + nx - 1] + b); }
    d[k] = v;
  }
  return d;
}
