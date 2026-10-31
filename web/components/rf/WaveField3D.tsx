"use client";
import { useFrame } from "@react-three/fiber";
import { reducedMotion, STILL_PHASE } from "../../lib/hooks";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { buildLut, SIGNED_STOPS, FIELD_STOPS } from "../../lib/ramp.mjs";
import type { CurrentData, FieldSlice, PulseData } from "../../lib/rf-data";

const vert = /* glsl */ `
varying vec2 vUv;
void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`;

// Phase rotation of the complex field: v(t) = Re{ E * exp(-j w t) }. Real solver data only.
const frag = /* glsl */ `
precision highp float;
varying vec2 vUv;
uniform sampler2D uField;
uniform sampler2D uLut;
uniform float uPhase;
uniform float uGamma;
uniform float uOpacity;
uniform float uFade;
void main(){
  vec2 c = texture2D(uField, vUv).rg;
  float v = c.r * cos(uPhase) - c.g * sin(uPhase);
  float s = sign(v) * pow(abs(v), uGamma);
  float t = clamp(0.5 + 0.5 * s, 0.0, 1.0);
  vec3 col = texture2D(uLut, vec2(t, 0.5)).rgb;
  vec2 e = min(vUv, 1.0 - vUv);
  float edge = smoothstep(0.0, uFade, min(e.x, e.y));
  float a = smoothstep(0.02, 0.5, abs(s)) * uOpacity * edge;
  gl_FragColor = vec4(col, a);
}`;

const lutTex = (stops: [number, [number, number, number]][]) => {
  const t = new THREE.DataTexture(buildLut(stops), 256, 1, THREE.RGBAFormat);
  t.minFilter = t.magFilter = THREE.LinearFilter;
  t.needsUpdate = true;
  return t;
};

function halfTex(re: Float32Array, im: Float32Array, nx: number, ny: number) {
  const d = new Uint16Array(nx * ny * 4);
  for (let i = 0; i < nx * ny; i++) {
    d[i * 4] = THREE.DataUtils.toHalfFloat(re[i]);
    d[i * 4 + 1] = THREE.DataUtils.toHalfFloat(im[i]);
    d[i * 4 + 3] = THREE.DataUtils.toHalfFloat(1);
  }
  const t = new THREE.DataTexture(d, nx, ny, THREE.RGBAFormat, THREE.HalfFloatType);
  t.minFilter = t.magFilter = THREE.LinearFilter;
  t.needsUpdate = true;
  return t;
}

/**
 * A slice of the solver's complex field, drawn as a glowing plane.
 * xy slices lie in the board plane at height `y`; xz slices stand vertically at board y = at_mm.
 */
export function WaveField3D({ slice, y, lift = 0, period = 3.4, opacity = 0.95, gamma = 0.5, paused, phase, seeThrough = false, yScale = 1 }: {
  slice: FieldSlice; y?: number; lift?: number; period?: number; opacity?: number; gamma?: number; paused?: boolean; phase?: number;
  /** draw over the board even where the plane lies inside it */ seeThrough?: boolean;
  /** vertical squash of the board model, for a vertical slice */ yScale?: number;
}) {
  const mat = useMemo(() => new THREE.ShaderMaterial({
    vertexShader: vert, fragmentShader: frag, transparent: true, depthWrite: false, depthTest: !seeThrough, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
    uniforms: {
      uField: { value: null }, uLut: { value: lutTex(SIGNED_STOPS) }, uPhase: { value: 0 }, uGamma: { value: gamma }, uOpacity: { value: opacity }, uFade: { value: 0.14 },
    },
  }), [gamma, opacity, seeThrough]);
  useEffect(() => {
    const tex = halfTex(slice.re, slice.im, slice.nx, slice.ny);
    mat.uniforms.uField.value = tex;
    return () => tex.dispose();
  }, [slice, mat]);
  const t = useRef((STILL_PHASE / (Math.PI * 2)) * period);
  useFrame((_, dt) => {
    if (phase !== undefined) { mat.uniforms.uPhase.value = phase; return; }
    // reduced motion: hold one phase of the wave instead of looping it
    if (!paused && !reducedMotion()) t.current += dt;
    mat.uniforms.uPhase.value = (t.current / period) * Math.PI * 2;
  });
  const w = slice.u[1] - slice.u[0];
  const h = slice.v[1] - slice.v[0];
  const cu = (slice.u[0] + slice.u[1]) / 2;
  const cv = (slice.v[0] + slice.v[1]) / 2;
  if (slice.plane === "xy") {
    // scenario (x, y, z) -> scene (x, z, -y). Texture rows run along +y, which the -90 degree turn maps to scene -z.
    return (
      <mesh position={[cu, (y ?? slice.at_mm) + lift, -cv]} rotation={[-Math.PI / 2, 0, 0]} material={mat} renderOrder={5}>
        <planeGeometry args={[w, h]} />
      </mesh>
    );
  }
  return (
    <mesh position={[cu, cv * yScale + lift, -slice.at_mm]} scale={[1, yScale, 1]} material={mat} renderOrder={5}>
      <planeGeometry args={[w, h]} />
    </mesh>
  );
}

/** Pulse launch: |E| frames played once over a plane. */
export function PulsePlane3D({ pulse, y, frame, fadeOut = 1, seeThrough = false }: { pulse: PulseData; y: number; frame: number; fadeOut?: number; seeThrough?: boolean }) {
  const lut = useMemo(() => lutTex(FIELD_STOPS), []);
  const mat = useMemo(() => new THREE.ShaderMaterial({
    vertexShader: vert,
    fragmentShader: /* glsl */ `
      varying vec2 vUv; uniform sampler2D uF; uniform sampler2D uLut; uniform float uFade;
      void main(){ float m = texture2D(uF, vUv).r; float s = pow(clamp(m,0.0,1.0), 0.5);
        vec3 c = texture2D(uLut, vec2(s,0.5)).rgb; vec2 e = min(vUv,1.0-vUv);
        gl_FragColor = vec4(c, smoothstep(0.02,0.5,s) * smoothstep(0.0,0.12,min(e.x,e.y)) * uFade); }`,
    transparent: true, depthWrite: false, depthTest: !seeThrough, blending: THREE.AdditiveBlending, uniforms: { uF: { value: null }, uLut: { value: lut }, uFade: { value: 1 } },
  }), [lut, seeThrough]);
  const tex = useMemo(() => {
    const t = new THREE.DataTexture(new Float32Array(pulse.nx * pulse.ny * 4), pulse.nx, pulse.ny, THREE.RGBAFormat, THREE.FloatType);
    t.minFilter = t.magFilter = THREE.LinearFilter;
    return t;
  }, [pulse]);
  useEffect(() => {
    const src = pulse.frames[Math.min(pulse.frames.length - 1, Math.max(0, frame))];
    const d = tex.image.data as Float32Array;
    for (let i = 0; i < src.length; i++) d[i * 4] = src[i];
    tex.needsUpdate = true;
    mat.uniforms.uF.value = tex;
  }, [frame, pulse, tex, mat]);
  mat.uniforms.uFade.value = fadeOut;
  const w = pulse.u[1] - pulse.u[0], h = pulse.v[1] - pulse.v[0];
  return (
    <mesh position={[(pulse.u[0] + pulse.u[1]) / 2, y, -(pulse.v[0] + pulse.v[1]) / 2]} rotation={[-Math.PI / 2, 0, 0]} material={mat} renderOrder={5}>
      <planeGeometry args={[w, h]} />
    </mesh>
  );
}

/** Surface current on the top copper: |Js| on a heat ramp, transparent off copper. Sits just above the copper plane. */
export function CurrentPlane3D({ data, y, glow = 1, seeThrough = false }: { data: CurrentData; y?: number; glow?: number; seeThrough?: boolean }) {
  const lut = useMemo(() => lutTex(FIELD_STOPS), []);
  const mat = useMemo(() => new THREE.ShaderMaterial({
    vertexShader: vert,
    fragmentShader: /* glsl */ `
      varying vec2 vUv; uniform sampler2D uF; uniform sampler2D uLut; uniform float uGlow;
      void main(){ float m = texture2D(uF, vUv).r; float s = pow(clamp(m,0.0,1.0), 0.62);
        vec3 c = texture2D(uLut, vec2(s,0.5)).rgb * (0.9 + 0.5 * uGlow * s);
        gl_FragColor = vec4(c, smoothstep(0.004, 0.05, m)); }`,
    transparent: true, depthWrite: false, depthTest: !seeThrough, uniforms: { uF: { value: null }, uLut: { value: lut }, uGlow: { value: glow } },
  }), [lut, glow, seeThrough]);
  const tex = useMemo(() => {
    const d = new Float32Array(data.nx * data.ny * 4);
    for (let i = 0; i < data.nx * data.ny; i++) d[i * 4] = data.mag[i];
    const t = new THREE.DataTexture(d, data.nx, data.ny, THREE.RGBAFormat, THREE.FloatType);
    t.minFilter = t.magFilter = THREE.LinearFilter;
    t.needsUpdate = true;
    return t;
  }, [data]);
  useEffect(() => { mat.uniforms.uF.value = tex; return () => tex.dispose(); }, [tex, mat]);
  const w = data.u[1] - data.u[0], h = data.v[1] - data.v[0];
  return (
    <mesh position={[(data.u[0] + data.u[1]) / 2, y ?? data.at_mm + 0.05, -(data.v[0] + data.v[1]) / 2]} rotation={[-Math.PI / 2, 0, 0]} material={mat} renderOrder={4}>
      <planeGeometry args={[w, h]} />
    </mesh>
  );
}
