"use client";
import { useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { sample, FIELD_STOPS } from "../../lib/ramp.mjs";
import type { PatternData } from "../../lib/rf-data";

/**
 * Balloon mesh from far-field directivity. Radius and colour follow strength above the floor.
 * Antenna-local axes: theta from +z (up), phi from +x toward +y (scenario axes).
 * Scene mapping: scenario (x, y, z) -> scene (x, z, -y).
 */
export function RadiationBalloon({ pattern, center, scale = 28, spin = false }: {
  pattern: PatternData; center: [number, number, number]; scale?: number; spin?: boolean;
}) {
  const ref = useRef<THREE.Group>(null);
  const { geo, maxPos } = useMemo(() => {
    const th = pattern.theta_deg, ph = pattern.phi_deg, floor = pattern.floor_dbi;
    const nt = th.length, np = ph.length;
    const pos: number[] = [], col: number[] = [], idx: number[] = [];
    let mx = new THREE.Vector3();
    for (let a = 0; a < nt; a++) {
      for (let b = 0; b < np; b++) {
        const d = pattern.dir_dbi[a][b];
        const t = Math.max(0, Math.min(1, (d - (pattern.dmax_dbi - 28)) / 28));
        const r = scale * Math.pow(t, 1.1) + 0.5;
        const T = (th[a] * Math.PI) / 180, P = (ph[b] * Math.PI) / 180;
        const sx = r * Math.sin(T) * Math.cos(P), sy = r * Math.sin(T) * Math.sin(P), sz = r * Math.cos(T);
        pos.push(sx, sz, -sy);
        const [cr, cg, cb] = sample(FIELD_STOPS, 0.04 + 0.96 * t);
        col.push(cr / 255, cg / 255, cb / 255);
        if (a === Math.round(pattern.dmax_theta_deg / (th[1] - th[0])) && b === Math.round(pattern.dmax_phi_deg / (ph[1] - ph[0]))) mx = new THREE.Vector3(sx, sz, -sy);
      }
    }
    for (let a = 0; a < nt - 1; a++) for (let b = 0; b < np - 1; b++) {
      const i = a * np + b;
      idx.push(i, i + np, i + 1, i + 1, i + np, i + np + 1);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("color", new THREE.Float32BufferAttribute(col, 3));
    g.setIndex(idx);
    g.computeVertexNormals();
    return { geo: g, maxPos: mx };
  }, [pattern, scale]);
  useFrame((_, dt) => { if (spin && ref.current) ref.current.rotation.y += dt * 0.25; });
  return (
    <group ref={ref} position={center}>
      <mesh geometry={geo} renderOrder={6}>
        <meshBasicMaterial vertexColors transparent opacity={0.82} side={THREE.DoubleSide} depthWrite={false} />
      </mesh>
      <mesh geometry={geo} renderOrder={7}>
        <meshBasicMaterial color="#ffd9a8" wireframe transparent opacity={0.07} depthWrite={false} />
      </mesh>
      <mesh position={maxPos}><sphereGeometry args={[0.9, 16, 16]} /><meshBasicMaterial color="#fff4d6" /></mesh>
    </group>
  );
}
