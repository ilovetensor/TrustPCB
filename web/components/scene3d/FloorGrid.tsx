"use client";
import { useMemo } from "react";
import * as THREE from "three";

const vert = /* glsl */ `
varying vec2 vXY;
void main(){
  vXY = position.xy;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0);
}`;
const frag = /* glsl */ `
varying vec2 vXY;
uniform vec3 uColor;
float line(float v, float w){
  float d = abs(fract(v - 0.5) - 0.5) / fwidth(v);
  return 1.0 - min(d / w, 1.0);
}
void main(){
  vec2 p = vXY;
  float minor = max(line(p.x, 1.0), line(p.y, 1.0));
  float mid = max(line(p.x / 10.0, 1.1), line(p.y / 10.0, 1.1));
  float major = max(line(p.x / 50.0, 1.4), line(p.y / 50.0, 1.4));
  float r = length(p);
  float fade = 1.0 - smoothstep(60.0, 260.0, r);
  float a = (minor * 0.05 + mid * 0.16 + major * 0.3) * fade;
  // soft pool of light under the board
  float pool = exp(-r * r / (2.0 * 55.0 * 55.0)) * 0.08;
  gl_FragColor = vec4(uColor, a + pool);
}`;

export function FloorGrid({ center, y = -3.2 }: { center: [number, number]; y?: number }) {
  const mat = useMemo(
    () => new THREE.ShaderMaterial({
      vertexShader: vert, fragmentShader: frag, transparent: true, depthWrite: false,
      uniforms: { uColor: { value: new THREE.Color("#8d86ff") } },
      extensions: {} as never,
    }),
    [],
  );
  return (
    <mesh position={[center[0], y, center[1]]} rotation={[-Math.PI / 2, 0, 0]} material={mat} renderOrder={-2}>
      <planeGeometry args={[900, 900]} />
    </mesh>
  );
}
