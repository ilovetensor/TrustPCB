// DEV-ONLY fixture generator. Parses a .kicad_pcb into the contracts.md board
// model (plus a `silk` array used only for drawing) so the web lane can build the
// 2D viewer before the PCB lane's renderer exists. Not part of the product.
//
//   node scripts/gen-board-fixture.mjs <in.kicad_pcb> <out.json>
import fs from "node:fs";

function parse(text) {
  let i = 0;
  const n = text.length;
  function node() {
    // at "("
    i++;
    const list = [];
    while (i < n) {
      const c = text[i];
      if (c === ")") {
        i++;
        return list;
      }
      if (c === "(") list.push(node());
      else if (c === '"') {
        let j = i + 1;
        let s = "";
        while (j < n && text[j] !== '"') {
          if (text[j] === "\\") j++;
          s += text[j++];
        }
        i = j + 1;
        list.push(s);
      } else if (/\s/.test(c)) i++;
      else {
        let j = i;
        while (j < n && !/[\s()]/.test(text[j])) j++;
        const tok = text.slice(i, j);
        i = j;
        list.push(/^-?\d+(\.\d+)?(e-?\d+)?$/i.test(tok) ? Number(tok) : tok);
      }
    }
    return list;
  }
  while (text[i] !== "(") i++;
  return node();
}
const kids = (l, k) => l.filter((x) => Array.isArray(x) && x[0] === k);
const kid = (l, k) => kids(l, k)[0];
const r3 = (v) => Math.round(v * 1000) / 1000;

const [, , inFile, outFile] = process.argv;
const root = parse(fs.readFileSync(inFile, "utf8"));
const short = (u) => String(u || "").replace(/-/g, "").slice(0, 8);

function xf(fx, fy, rot, side) {
  const t = (rot * Math.PI) / 180;
  const c = Math.cos(t);
  const s = Math.sin(t);
  return (px, py) => {
    const mx = side === "bottom" ? -px : px;
    return [fx + mx * c + py * s, fy - mx * s + py * c];
  };
}

function padRing(p) {
  const { x, y, w, h, rotation: rot, shape } = p;
  const t = (rot * Math.PI) / 180;
  const c = Math.cos(t);
  const s = Math.sin(t);
  const rotp = (px, py) => [r3(x + px * c + py * s), r3(y - px * s + py * c)];
  const pts = [];
  if (shape === "circle" || shape === "oval") {
    const N = 20;
    for (let k = 0; k < N; k++) {
      const a = (k / N) * Math.PI * 2;
      pts.push(rotp((Math.cos(a) * w) / 2, (Math.sin(a) * h) / 2));
    }
  } else {
    const hx = w / 2;
    const hy = h / 2;
    for (const [a, b] of [
      [-hx, -hy],
      [hx, -hy],
      [hx, hy],
      [-hx, hy],
    ])
      pts.push(rotp(a, b));
  }
  return pts;
}

const nets = new Map();
const addNet = (name) => {
  if (!name) return null;
  if (!nets.has(name)) nets.set(name, { id: `net:${name}`, name, net_class: "Default" });
  return `net:${name}`;
};

const layers = [];
for (const l of kid(root, "layers").slice(1)) {
  const name = l[1];
  let kind = "other";
  if (/\.Cu$/.test(name)) kind = "copper";
  else if (/SilkS/.test(name)) kind = "silk";
  else if (/Mask/.test(name)) kind = "mask";
  else if (/Paste/.test(name)) kind = "paste";
  else if (/Fab|CrtYd/.test(name)) kind = "fab";
  else if (name === "Edge.Cuts") kind = "edge";
  layers.push({ name, kind, index: l[0] });
}

const footprints = [];
const pads = [];
const silk = [];
for (const fp of kids(root, "footprint")) {
  const at = kid(fp, "at");
  const fx = at[1];
  const fy = at[2];
  const rot = at[3] || 0;
  const side = kid(fp, "layer")[1] === "B.Cu" ? "bottom" : "top";
  const props = {};
  for (const p of kids(fp, "property")) props[p[1]] = p[2];
  const ref = props.Reference || "?";
  const T = xf(fx, fy, rot, side);
  const padIds = [];
  let x0 = 1e9,
    y0 = 1e9,
    x1 = -1e9,
    y1 = -1e9;
  const seen = new Map();
  for (const pd of kids(fp, "pad")) {
    const number = String(pd[1]);
    const pat = kid(pd, "at");
    const size = kid(pd, "size");
    const [px, py] = T(pat[1], pat[2]);
    const padRot = (pat[3] ?? rot) || 0;
    const nm = kid(pd, "net");
    const drillN = kid(pd, "drill");
    let drill = null;
    if (drillN) drill = typeof drillN[1] === "number" ? drillN[1] : (drillN.find((v) => typeof v === "number") ?? null);
    const k = (seen.get(number) || 0) + 1;
    seen.set(number, k);
    const pad = {
      id: `pad:${ref}:${number}${k > 1 ? "." + k : ""}`,
      ref,
      number,
      net: nm ? addNet(nm[1]) : null,
      x: r3(px),
      y: r3(py),
      w: size[1],
      h: size[2],
      rotation: padRot,
      shape: pd[3],
      layers: (kid(pd, "layers") || []).slice(1),
      drill,
    };
    pad.ring = padRing(pad);
    for (const [qx, qy] of pad.ring) {
      x0 = Math.min(x0, qx);
      y0 = Math.min(y0, qy);
      x1 = Math.max(x1, qx);
      y1 = Math.max(y1, qy);
    }
    pads.push(pad);
    padIds.push(pad.id);
  }
  // silk graphics
  for (const g of fp.filter((e) => Array.isArray(e) && /^fp_(line|circle|arc|rect|poly)$/.test(e[0]))) {
    const layer = kid(g, "layer")?.[1];
    if (!layer || !/SilkS$/.test(layer)) continue;
    const kind = g[0].slice(3);
    const sw = kid(g, "stroke")?.[1]?.[1] ?? 0.12;
    if (kind === "line") {
      const a = kid(g, "start");
      const b = kid(g, "end");
      silk.push({ layer, kind: "line", pts: [T(a[1], a[2]).map(r3), T(b[1], b[2]).map(r3)], w: sw });
    } else if (kind === "rect") {
      const a = kid(g, "start");
      const b = kid(g, "end");
      silk.push({ layer, kind: "poly", pts: [[a[1], a[2]], [b[1], a[2]], [b[1], b[2]], [a[1], b[2]]].map(([u, v]) => T(u, v).map(r3)), w: sw, closed: true });
    } else if (kind === "poly") {
      const pts = kid(g, "pts")
        .filter((e) => e[0] === "xy")
        .map((e) => T(e[1], e[2]).map(r3));
      silk.push({ layer, kind: "poly", pts, w: sw, closed: true });
    } else if (kind === "circle") {
      const c = kid(g, "center");
      const e = kid(g, "end");
      const rad = Math.hypot(e[1] - c[1], e[2] - c[2]);
      const [cx, cy] = T(c[1], c[2]);
      silk.push({ layer, kind: "circle", c: [r3(cx), r3(cy)], r: r3(rad), w: sw });
    } else if (kind === "arc") {
      const a = kid(g, "start");
      const m = kid(g, "mid");
      const b = kid(g, "end");
      silk.push({ layer, kind: "arc", pts: [T(a[1], a[2]), T(m[1], m[2]), T(b[1], b[2])].map((p) => p.map(r3)), w: sw });
    }
  }
  // courtyard
  let courtyard = null;
  const cy = fp.filter((e) => Array.isArray(e) && /^fp_(rect|poly|line)$/.test(e[0]) && /CrtYd$/.test(kid(e, "layer")?.[1] || ""));
  const cpts = [];
  for (const g of cy) {
    if (g[0] === "fp_rect") {
      const a = kid(g, "start");
      const b = kid(g, "end");
      cpts.push(T(a[1], a[2]), T(b[1], b[2]), T(a[1], b[2]), T(b[1], a[2]));
    } else if (g[0] === "fp_line") {
      const a = kid(g, "start");
      const b = kid(g, "end");
      cpts.push(T(a[1], a[2]), T(b[1], b[2]));
    }
  }
  if (cpts.length) {
    const xs = cpts.map((p) => p[0]);
    const ys = cpts.map((p) => p[1]);
    const cx0 = Math.min(...xs), cx1 = Math.max(...xs), cy0 = Math.min(...ys), cy1 = Math.max(...ys);
    courtyard = [[cx0, cy0], [cx1, cy0], [cx1, cy1], [cx0, cy1]].map((p) => p.map(r3));
    x0 = Math.min(x0, cx0); y0 = Math.min(y0, cy0); x1 = Math.max(x1, cx1); y1 = Math.max(y1, cy1);
  }
  if (!isFinite(x0)) { x0 = fx - 1; y0 = fy - 1; x1 = fx + 1; y1 = fy + 1; }
  footprints.push({
    id: `ref:${ref}`,
    ref,
    value: props.Value || "",
    lib_id: fp[1],
    x: fx,
    y: fy,
    rotation: rot,
    side,
    bbox: [r3(x0), r3(y0), r3(x1), r3(y1)],
    courtyard,
    pad_ids: padIds,
    dnp: false,
  });
}

const tracks = [];
for (const s of kids(root, "segment")) {
  const a = kid(s, "start");
  const b = kid(s, "end");
  tracks.push({
    id: `track:${short(kid(s, "uuid")[1])}`,
    net: addNet(kid(s, "net")?.[1]),
    layer: kid(s, "layer")[1],
    width: kid(s, "width")[1],
    start: [a[1], a[2]],
    end: [b[1], b[2]],
    mid: null,
    length: r3(Math.hypot(b[1] - a[1], b[2] - a[2])),
  });
}
for (const s of kids(root, "arc")) {
  const a = kid(s, "start");
  const m = kid(s, "mid");
  const b = kid(s, "end");
  tracks.push({
    id: `track:${short(kid(s, "uuid")[1])}`,
    net: addNet(kid(s, "net")?.[1]),
    layer: kid(s, "layer")[1],
    width: kid(s, "width")[1],
    start: [a[1], a[2]],
    end: [b[1], b[2]],
    mid: [m[1], m[2]],
    length: r3(Math.hypot(b[1] - a[1], b[2] - a[2]) * 1.05),
  });
}
const vias = kids(root, "via").map((v) => {
  const at = kid(v, "at");
  return {
    id: `via:${short(kid(v, "uuid")[1])}`,
    net: addNet(kid(v, "net")?.[1]),
    x: at[1],
    y: at[2],
    diameter: kid(v, "size")[1],
    drill: kid(v, "drill")[1],
    layers: kid(v, "layers").slice(1),
  };
});

const zones = [];
const keepouts = [];
for (const z of kids(root, "zone")) {
  const lyr = kid(z, "layers") ? kid(z, "layers").slice(1) : [kid(z, "layer")[1]];
  const isKeepout = !!kid(z, "keepout");
  if (isKeepout) {
    const poly = kid(kid(z, "polygon"), "pts")
      .filter((e) => e[0] === "xy")
      .map((e) => [e[1], e[2]]);
    keepouts.push({ id: `keepout:${short(kid(z, "uuid")[1])}`, name: kid(z, "name")?.[1], layers: lyr, ring: poly, rules: {} });
    continue;
  }
  const filled = {};
  for (const fpoly of kids(z, "filled_polygon")) {
    const L = kid(fpoly, "layer")[1];
    const ring = kid(fpoly, "pts")
      .filter((e) => e[0] === "xy")
      .map((e) => [r3(e[1]), r3(e[2])]);
    (filled[L] ||= []).push(ring);
  }
  zones.push({ id: `zone:${short(kid(z, "uuid")[1])}`, net: addNet(kid(z, "net")?.[1]), name: kid(z, "name")?.[1], layers: lyr, filled, is_keepout: false });
}

// outline from Edge.Cuts
const edge = [];
for (const g of root.filter((e) => Array.isArray(e) && /^gr_(line|arc)$/.test(e[0]) && kid(e, "layer")?.[1] === "Edge.Cuts")) {
  const a = kid(g, "start");
  const b = kid(g, "end");
  if (g[0] === "gr_arc") {
    const m = kid(g, "mid");
    edge.push({ kind: "arc", a: [a[1], a[2]], m: [m[1], m[2]], b: [b[1], b[2]] });
  } else edge.push({ kind: "line", a: [a[1], a[2]], b: [b[1], b[2]] });
}
// Edge segments in file order are not chained; sample each one into a ring ordered by chaining.
const sample = (e) => {
  if (e.kind === "line") return [e.a, e.b];
  const [ax, ay] = e.a, [mx, my] = e.m, [bx, by] = e.b;
  const d = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my));
  const ux = ((ax ** 2 + ay ** 2) * (my - by) + (mx ** 2 + my ** 2) * (by - ay) + (bx ** 2 + by ** 2) * (ay - my)) / d;
  const uy = ((ax ** 2 + ay ** 2) * (bx - mx) + (mx ** 2 + my ** 2) * (ax - bx) + (bx ** 2 + by ** 2) * (mx - ax)) / d;
  const R = Math.hypot(ax - ux, ay - uy);
  let a0 = Math.atan2(ay - uy, ax - ux);
  let a1 = Math.atan2(by - uy, bx - ux);
  const am = Math.atan2(my - uy, mx - ux);
  const norm = (v) => ((v % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
  let sweep = norm(a1 - a0);
  const mid = norm(am - a0);
  if (mid > sweep) sweep -= 2 * Math.PI;
  const pts = [];
  for (let k = 0; k <= 8; k++) {
    const t = a0 + (sweep * k) / 8;
    pts.push([ux + R * Math.cos(t), uy + R * Math.sin(t)]);
  }
  return pts;
};
const remaining = edge.map(sample);
let ring = remaining.shift();
const close = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1]) < 0.01;
while (remaining.length) {
  const tail = ring[ring.length - 1];
  const idx = remaining.findIndex((s) => close(s[0], tail) || close(s[s.length - 1], tail));
  if (idx < 0) break;
  let s = remaining.splice(idx, 1)[0];
  if (!close(s[0], tail)) s = s.slice().reverse();
  ring = ring.concat(s.slice(1));
}
ring = ring.map((p) => p.map(r3));
const xs = ring.map((p) => p[0]);
const ys = ring.map((p) => p[1]);

// silk text
for (const t of kids(root, "gr_text")) {
  const layer = kid(t, "layer")?.[1];
  if (!/SilkS/.test(layer || "")) continue;
  const at = kid(t, "at");
  silk.push({ layer, kind: "text", text: t[1], x: at[1], y: at[2], rot: at[3] || 0, size: kid(kid(kid(t, "effects"), "font"), "size")?.[1] ?? 1 });
}

const out = {
  version_id: "fixture-nrf-v1",
  title: "nRF52840 RF IoT Node",
  outline: [ring],
  bbox: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)],
  layers,
  stackup: { source: "board", assumed: false, layers: [] },
  nets: [...nets.values()],
  footprints,
  pads,
  tracks,
  vias,
  zones,
  keepouts,
  silk,
  _fixture: { dev_only: true, note: "Generated from kicad_pcb by scripts/gen-board-fixture.mjs. Replace by PCB lane renderer output." },
};
fs.writeFileSync(outFile, JSON.stringify(out));
console.log(JSON.stringify({ footprints: footprints.length, pads: pads.length, tracks: tracks.length, vias: vias.length, zones: zones.length, keepouts: keepouts.length, bbox: out.bbox, nets: nets.size, silk: silk.length }));
