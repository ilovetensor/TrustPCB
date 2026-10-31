import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import type { Board, Footprint } from "../../lib/types";

export type LayerGroupKey = "components" | "topCu" | "substrate" | "botCu";

export interface BoardModel {
  groups: Record<LayerGroupKey, THREE.Group>;
  /** mm, board coords: x, board y (= GLB z). */
  bboxMm: { x0: number; x1: number; z0: number; z1: number; top: number; bottom: number };
}

const palette = {
  // dark charcoal mask, matte enough that no lamp leaves a white patch
  substrate: new THREE.MeshPhysicalMaterial({ color: "#121316", roughness: 0.72, metalness: 0.05, clearcoat: 0.12, clearcoatRoughness: 0.55 }),
  // copper under mask reads warmer and lighter than the laminate, so tracks stand out from it
  copper: new THREE.MeshPhysicalMaterial({ color: "#403c38", roughness: 0.42, metalness: 0.3, clearcoat: 0.25, clearcoatRoughness: 0.4 }),
  pad: new THREE.MeshStandardMaterial({ color: "#f0d9a0", roughness: 0.3, metalness: 0.9 }),
  silk: new THREE.MeshStandardMaterial({ color: "#f3efd2", roughness: 0.8, metalness: 0 }),
};

const TARGET_THICKNESS = 1.6;
/** Total thickness of the stack-up in mm, 0 when the file does not say. */
export function stackThickness(board: Board): number {
  let t = 0;
  for (const l of board.stackup.layers as { thickness_mm?: number | null }[]) if (typeof l.thickness_mm === "number") t += l.thickness_mm;
  return t;
}
/**
 * Vertical squash that draws a thick stack-up (KiCad's default 4-layer file is 4.69 mm) at a normal 1.6 mm, so the board does
 * not look like a brick and the overlays that sit at 1.6 mm still land on its surface. 1 for ordinary boards.
 */
export function boardYScale(board: Board): number {
  const t = stackThickness(board);
  return t > 2.2 ? TARGET_THICKNESS / t : 1;
}

/**
 * kicad-cli gives its board materials fixed base colours: PCB dark green, copper yellow, pads 50 % grey, silkscreen translucent white.
 * glTF node names win over mesh names in the loader, so the mesh names below often do not reach us; the colours always do.
 */
function boardPartByColor(m: THREE.Material): "pcb" | "copper" | "pad" | "silk" | null {
  const sm = m as THREE.MeshStandardMaterial;
  if (!sm.color) return null;
  const c = sm.color.getRGB({ r: 0, g: 0, b: 0 }, THREE.LinearSRGBColorSpace);
  if (sm.opacity < 0.99 || (c.r > 0.9 && c.g > 0.9 && c.b > 0.9)) return "silk";
  if (c.g > c.r && c.g > c.b && c.r < 0.2) return "pcb";
  if (Math.abs(c.r - c.g) < 0.03 && Math.abs(c.g - c.b) < 0.03 && c.r > 0.3 && c.r < 0.7) return "pad";
  if (c.r > 0.5 && c.g > 0.4 && c.b < 0.2) return "copper";
  return null;
}

/** kicad-cli names its board meshes <board>_PCB, _copper, _pad, _via and _silkscreen. Material order is not stable, mesh names are. */
function boardPart(obj: THREE.Object3D): "pcb" | "copper" | "pad" | "via" | "silk" | null {
  for (let p: THREE.Object3D | null = obj; p; p = p.parent) {
    const m = /_(PCB|copper|pad|via|silkscreen)$/.exec(p.name || "");
    if (m) return ({ PCB: "pcb", copper: "copper", pad: "pad", via: "via", silkscreen: "silk" } as const)[m[1] as "PCB"];
  }
  return null;
}

function gradePart(src: THREE.Material): THREE.Material {
  const m = src as THREE.MeshStandardMaterial;
  const c = (m.color ?? new THREE.Color("#888")).clone();
  const hsl = { h: 0, s: 0, l: 0 };
  c.getHSL(hsl);
  // pull toward the bench palette: slightly desaturated, ink-friendly lows
  c.setHSL(hsl.h, hsl.s * 0.72, Math.min(0.88, hsl.l * 1.0));
  const metal = hsl.l > 0.55 && hsl.s < 0.2;
  return new THREE.MeshStandardMaterial({ color: c, roughness: metal ? 0.3 : 0.62, metalness: metal ? 0.85 : 0.08 });
}

/** Merge thousands of GLB primitives into a handful of meshes, grouped by exploded layer. */
export function buildBoardModel(scene: THREE.Object3D, board: Board): BoardModel {
  const refs = new Set(board.footprints.map((f) => f.ref));
  const ky = boardYScale(board);
  const rise = ky === 1 ? 0 : stackThickness(board) - TARGET_THICKNESS; // how far a thick board's top face drops
  const seenRefs = new Set<string>();
  const mid = 0.5 * (ky === 1 ? Math.max(TARGET_THICKNESS, stackThickness(board)) : TARGET_THICKNESS);
  scene.updateMatrixWorld(true);
  const buckets = new Map<string, { group: LayerGroupKey; ref?: string; material: THREE.Material; geoms: THREE.BufferGeometry[] }>();
  const partMats = new Map<string, THREE.Material>();
  const box = new THREE.Box3();
  const mmBox = new THREE.Box3();

  scene.traverse((o) => {
    const mesh = o as THREE.Mesh;
    if (!mesh.isMesh) return;
    let ref: string | undefined;
    for (let p: THREE.Object3D | null = mesh; p; p = p.parent) {
      if (refs.has(p.name)) { ref = p.name; break; }
    }
    const g = mesh.geometry.clone();
    g.applyMatrix4(mesh.matrixWorld);
    g.scale(1000, 1000, 1000); // metres -> millimetres, x stays x, GLB z is board y
    if (ky !== 1) {
      // thick stack-up: squash the board's own layers, and lower the parts that stand on the top face by the same drop
      if (ref) { const pos = g.attributes.position; for (let i = 0; i < pos.count; i++) if (pos.getY(i) > 0.5 * stackThickness(board)) pos.setY(i, pos.getY(i) - rise); }
      else g.scale(1, ky, 1);
    }
    for (const k of Object.keys(g.attributes)) if (k !== "position" && k !== "normal") g.deleteAttribute(k);
    if (!g.attributes.normal) g.computeVertexNormals();
    g.computeBoundingBox();
    box.copy(g.boundingBox!);
    mmBox.union(box);
    const matName = (mesh.material as THREE.Material).name || "";
    let group: LayerGroupKey;
    let material: THREE.Material;
    if (ref) {
      seenRefs.add(ref);
      group = "components";
      const key = (mesh.material as THREE.Material).uuid;
      if (!partMats.has(key)) partMats.set(key, gradePart(mesh.material as THREE.Material));
      material = partMats.get(key)!;
    } else {
      const cy = (box.min.y + box.max.y) / 2;
      const h = box.max.y - box.min.y;
      const part = boardPart(mesh) ?? boardPartByColor(mesh.material as THREE.Material);
      const level: LayerGroupKey = h > 0.5 ? "substrate" : cy > 0.8 ? "topCu" : "botCu";
      if (part === "pcb" || (!part && matName === "mat_18")) { group = "substrate"; material = palette.substrate; }
      else if (part === "silk") { group = cy > mid ? "topCu" : "botCu"; material = palette.silk; }
      else if (part === "pad" || (!part && matName === "mat_17")) { material = palette.pad; group = level; }
      else { material = palette.copper; group = level; }
    }
    const key = `${group}|${ref ?? ""}|${material.uuid}`;
    let b = buckets.get(key);
    if (!b) buckets.set(key, (b = { group, ref, material, geoms: [] }));
    b.geoms.push(g);
  });

  // parts the GLB has no model for (no 3D model in the footprint, or kicad-cli could not find the library) get plain bodies
  const bodyMats = new Map<string, THREE.Material>();
  for (const b of fallbackBodies(board, seenRefs, ky, { top: 1.6, bottom: -0.04 })) {
    let m = bodyMats.get(b.color);
    if (!m) bodyMats.set(b.color, (m = new THREE.MeshStandardMaterial({ color: b.color, roughness: 0.62, metalness: 0.12 })));
    buckets.set(`components|${b.ref}|${m.uuid}`, { group: "components", ref: b.ref, material: m, geoms: [b.geom] });
  }

  const groups: Record<LayerGroupKey, THREE.Group> = {
    components: new THREE.Group(), topCu: new THREE.Group(), substrate: new THREE.Group(), botCu: new THREE.Group(),
  };
  for (const b of buckets.values()) {
    const merged = mergeGeometries(b.geoms, false);
    b.geoms.forEach((g) => g.dispose());
    if (!merged) continue;
    const m = new THREE.Mesh(merged, b.material);
    if (b.ref) m.userData.ref = b.ref;
    groups[b.group].add(m);
  }
  return {
    groups,
    bboxMm: { x0: mmBox.min.x, x1: mmBox.max.x, z0: mmBox.min.z, z1: mmBox.max.z, top: 1.54, bottom: -0.04 },
  };
}

/* ---------------- fallback bodies ---------------- */

interface Body { h: number; color: string; inset?: number }

/** Plain body height and colour by package, from the footprint's library name and reference. */
function bodyFor(f: Footprint): Body | null {
  const lib = f.lib_id.toLowerCase(), name = (lib.split(":").pop() ?? "").toLowerCase(), ref = f.ref.toUpperCase();
  if (/^(mountinghole|fiducial|testpoint|logo|symbol|kicad-logo)/.test(lib) || /^(h|fid|tp|mh|g|logo)\d*$/i.test(ref) || /mountinghole|fiducial|testpoint|logo/.test(name)) return null;
  const size = /(\d{4})_\d{4}metric/.exec(name)?.[1];
  const passive = (thin: number, mid: number, big: number) => (size === "0201" || size === "0402" ? thin : size === "0603" ? mid : big);
  if (lib.startsWith("capacitor_smd") || lib.startsWith("capacitor_tantalum")) {
    if (/cp_elec/.test(name)) return { h: 5.5, color: "#4a4d55" };
    return { h: passive(0.5, 0.85, 1.1), color: "#9a8466" };
  }
  if (lib.startsWith("resistor_smd") || lib.startsWith("fuse")) return { h: passive(0.35, 0.5, 0.6), color: "#2c2d30" };
  if (lib.startsWith("led_smd")) return { h: 0.8, color: "#d8d6cc" };
  if (lib.startsWith("inductor") || lib.startsWith("ferrite")) return { h: 1.6, color: "#3a3b3f" };
  if (lib.startsWith("diode")) return { h: 1.1, color: "#2c2d30" };
  if (lib.startsWith("crystal") || lib.startsWith("oscillator")) return { h: 1.0, color: "#b8b9bd" };
  if (lib.startsWith("package_to_sot") || lib.startsWith("package_sot")) return { h: 1.25, color: "#26272a" };
  if (lib.startsWith("button_switch") || lib.startsWith("switch")) return { h: 3.4, color: "#c8c8cc" };
  if (/usb|rj45|barrel|jack/.test(lib)) return { h: 4.5, color: "#b4b5ba" };
  if (lib.startsWith("connector_pin") || /pinheader|pinsocket/.test(lib)) return { h: 6, color: "#2d2e33", inset: 0.1 };
  if (lib.startsWith("connector")) return { h: 5.5, color: "#c9c9cc" };
  if (lib.startsWith("jumper") || lib.startsWith("solderjumper")) return { h: 0.15, color: "#5a5a60" };
  if (lib.startsWith("rf_module") || /esp32|module/.test(lib + name)) return { h: 3.2, color: "#b6b7bc" };
  if (lib.startsWith("rf_antenna") || /antenna/.test(name)) return { h: 0.2, color: "#7a6244" };
  if (lib.startsWith("package_") || /qfn|qfp|soic|dfn|bga|lga|tssop|msop|wlcsp/.test(name)) return { h: 1.0, color: "#26272a" };
  const w = f.bbox[2] - f.bbox[0], d = f.bbox[3] - f.bbox[1];
  return { h: Math.min(3.5, Math.max(0.8, Math.sqrt(w * d) * 0.14)), color: "#34363a" };
}

/** Simple boxes standing in for 3D models the GLB did not carry, from each footprint's courtyard. */
function fallbackBodies(board: Board, have: Set<string>, scale: number, tops: { top: number; bottom: number }): { ref: string; geom: THREE.BufferGeometry; color: string }[] {
  const out: { ref: string; geom: THREE.BufferGeometry; color: string }[] = [];
  for (const f of board.footprints) {
    if (have.has(f.ref)) continue;
    const body = bodyFor(f);
    if (!body) continue;
    const pts = f.courtyard && f.courtyard.length >= 3 ? f.courtyard : null;
    let x0 = f.bbox[0], y0 = f.bbox[1], x1 = f.bbox[2], y1 = f.bbox[3];
    if (pts) { x0 = Math.min(...pts.map((p) => p[0])); x1 = Math.max(...pts.map((p) => p[0])); y0 = Math.min(...pts.map((p) => p[1])); y1 = Math.max(...pts.map((p) => p[1])); }
    const w0 = x1 - x0, d0 = y1 - y0;
    if (!(w0 > 0.3 && d0 > 0.3)) continue;
    // the courtyard is a margin around the part; the body is inside it
    const inset = body.inset ?? Math.min(0.28, 0.16 * Math.min(w0, d0));
    const w = Math.max(0.2, w0 - 2 * inset), d = Math.max(0.2, d0 - 2 * inset), h = body.h;
    const g = new THREE.BoxGeometry(w, h, d);
    const y = f.side === "bottom" ? tops.bottom - h / 2 : tops.top + h / 2;
    g.translate((x0 + x1) / 2, y, (y0 + y1) / 2);
    out.push({ ref: f.ref, geom: g, color: body.color });
  }
  void scale;
  return out;
}
