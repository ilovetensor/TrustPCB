import * as THREE from "three";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";

export type LayerGroupKey = "components" | "topCu" | "substrate" | "botCu";

export interface BoardModel {
  groups: Record<LayerGroupKey, THREE.Group>;
  /** mm, board coords: x, board y (= GLB z). */
  bboxMm: { x0: number; x1: number; z0: number; z1: number; top: number; bottom: number };
}

const palette = {
  substrate: new THREE.MeshPhysicalMaterial({ color: "#26272a", roughness: 0.5, metalness: 0.15, clearcoat: 0.7, clearcoatRoughness: 0.35 }),
  copper: new THREE.MeshPhysicalMaterial({ color: "#303236", roughness: 0.3, metalness: 0.55, clearcoat: 1, clearcoatRoughness: 0.18 }),
  pad: new THREE.MeshStandardMaterial({ color: "#f0d9a0", roughness: 0.2, metalness: 1 }),
  silk: new THREE.MeshStandardMaterial({ color: "#f3efd2", roughness: 0.8, metalness: 0 }),
};

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
export function buildBoardModel(scene: THREE.Object3D, refs: Set<string>): BoardModel {
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
    for (const k of Object.keys(g.attributes)) if (k !== "position" && k !== "normal") g.deleteAttribute(k);
    if (!g.attributes.normal) g.computeVertexNormals();
    g.computeBoundingBox();
    box.copy(g.boundingBox!);
    mmBox.union(box);
    const matName = (mesh.material as THREE.Material).name || "";
    let group: LayerGroupKey;
    let material: THREE.Material;
    if (ref) {
      group = "components";
      const key = (mesh.material as THREE.Material).uuid;
      if (!partMats.has(key)) partMats.set(key, gradePart(mesh.material as THREE.Material));
      material = partMats.get(key)!;
    } else {
      const cy = (box.min.y + box.max.y) / 2;
      const h = box.max.y - box.min.y;
      const part = boardPart(mesh);
      const level: LayerGroupKey = h > 0.5 ? "substrate" : cy > 0.8 ? "topCu" : "botCu";
      if (part === "pcb" || (!part && matName === "mat_18")) { group = "substrate"; material = palette.substrate; }
      else if (part === "silk") { group = "topCu"; material = palette.silk; }
      else if (part === "pad" || (!part && matName === "mat_17")) { material = palette.pad; group = level; }
      else { material = palette.copper; group = level; }
    }
    const key = `${group}|${ref ?? ""}|${material.uuid}`;
    let b = buckets.get(key);
    if (!b) buckets.set(key, (b = { group, ref, material, geoms: [] }));
    b.geoms.push(g);
  });

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
