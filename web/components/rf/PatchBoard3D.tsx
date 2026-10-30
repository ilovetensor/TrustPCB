"use client";
import { useMemo } from "react";
import * as THREE from "three";
import type { RfBundle } from "../../lib/rf-data";

/** The board the solver actually simulated, drawn from scenario.json (scene = (x, z, -y)). */
export function PatchBoard3D({ rf }: { rf: RfBundle }) {
  const sc = rf.scenario;
  const mats = useMemo(() => ({
    sub: new THREE.MeshPhysicalMaterial({ color: "#07071a", roughness: 0.62, metalness: 0.1, clearcoat: 0.35, clearcoatRoughness: 0.5 }),
    cu: new THREE.MeshStandardMaterial({ color: "#e8c97a", roughness: 0.3, metalness: 1 }),
    gnd: new THREE.MeshStandardMaterial({ color: "#8a6a46", roughness: 0.4, metalness: 0.9 }),
  }), []);
  const [x0, y0, x1, y1] = sc.substrate.box;
  const th = sc.substrate.zmax - sc.substrate.zmin;
  return (
    <group>
      <mesh position={[(x0 + x1) / 2, th / 2, -(y0 + y1) / 2]} material={mats.sub}><boxGeometry args={[x1 - x0, th, y1 - y0]} /></mesh>
      {sc.metals.map((m) => m.rects.map((r, i) => (
        <mesh key={m.name + i} position={[(r[0] + r[2]) / 2, m.z_mm + (m.z_mm > 0.5 ? 0.02 : -0.02), -(r[1] + r[3]) / 2]} material={m.z_mm > 0.5 ? mats.cu : mats.gnd}>
          <boxGeometry args={[r[2] - r[0], 0.045, r[3] - r[1]]} />
        </mesh>
      )))}
      {sc.port && (
        <mesh position={[sc.port.start[0], th / 2, -sc.port.start[1]]} material={mats.cu}>
          <cylinderGeometry args={[0.45, 0.45, th + 0.1, 16]} />
        </mesh>
      )}
    </group>
  );
}
