"use client";
import { reducedMotion, STILL_PHASE } from "../../lib/hooks";
import { useEffect, useRef } from "react";
import { buildLut, FIELD_STOPS, SIGNED_STOPS } from "../../lib/ramp.mjs";
import type { CurrentData, FieldSlice, PulseData } from "../../lib/rf-data";

const signedLut = buildLut(SIGNED_STOPS);
const fieldLut = buildLut(FIELD_STOPS);

/** Phase-rotating field drawn in board millimetres (scenario y is up, so the canvas is flipped). For use inside Board2D's overlay slot. */
export function FieldCanvas2D({ slice, period = 3.4, opacity = 0.9, paused }: { slice: FieldSlice; period?: number; opacity?: number; paused?: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  useEffect(() => {
    const cv = ref.current!;
    const ctx = cv.getContext("2d")!;
    const img = ctx.createImageData(slice.nx, slice.ny);
    const still = reducedMotion();
    let raf = 0, t = still ? (STILL_PHASE / (Math.PI * 2)) * period : 0, last = performance.now();
    const draw = () => {
      const now = performance.now();
      if (!pausedRef.current && !still) t += (now - last) / 1000;
      last = now;
      const ph = (t / period) * Math.PI * 2, c = Math.cos(ph), s = Math.sin(ph);
      const d = img.data;
      for (let i = 0; i < slice.nx * slice.ny; i++) {
        const v = slice.re[i] * c - slice.im[i] * s;
        const q = Math.sign(v) * Math.pow(Math.abs(v), 0.62);
        const k = Math.max(0, Math.min(255, Math.round((0.5 + 0.5 * q) * 255)));
        const a = Math.min(1, Math.max(0, (Math.abs(q) - 0.015) / 0.4));
        d[i * 4] = signedLut[k * 4]; d[i * 4 + 1] = signedLut[k * 4 + 1]; d[i * 4 + 2] = signedLut[k * 4 + 2];
        d[i * 4 + 3] = a * a * (3 - 2 * a) * 255 * opacity;
      }
      ctx.putImageData(img, 0, 0);
      if (!still) raf = requestAnimationFrame(draw);
    };
    draw();
    return () => cancelAnimationFrame(raf);
  }, [slice, period, opacity]);
  return (
    <canvas
      ref={ref} width={slice.nx} height={slice.ny}
      style={{ position: "absolute", left: slice.u[0], top: -slice.v[1], width: slice.u[1] - slice.u[0], height: slice.v[1] - slice.v[0], transform: "scaleY(-1)", mixBlendMode: "screen" }}
    />
  );
}

/** Surface current heat over copper (|Js| / scale). */
export function CurrentCanvas2D({ data, opacity = 1 }: { data: CurrentData; opacity?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const cv = ref.current!;
    const ctx = cv.getContext("2d")!;
    const img = ctx.createImageData(data.nx, data.ny);
    for (let i = 0; i < data.nx * data.ny; i++) {
      const m = Math.min(1, data.mag[i]);
      const k = Math.round(Math.pow(m, 0.7) * 255);
      img.data[i * 4] = fieldLut[k * 4]; img.data[i * 4 + 1] = fieldLut[k * 4 + 1]; img.data[i * 4 + 2] = fieldLut[k * 4 + 2];
      img.data[i * 4 + 3] = data.mag[i] > 0.003 ? 255 * opacity : 0;
    }
    ctx.putImageData(img, 0, 0);
  }, [data, opacity]);
  return <canvas ref={ref} width={data.nx} height={data.ny} style={{ position: "absolute", left: data.u[0], top: -data.v[1], width: data.u[1] - data.u[0], height: data.v[1] - data.v[0], transform: "scaleY(-1)" }} />;
}

/** One pulse frame in board millimetres. */
export function PulseCanvas2D({ pulse, frame }: { pulse: PulseData; frame: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const cv = ref.current!;
    const ctx = cv.getContext("2d")!;
    const img = ctx.createImageData(pulse.nx, pulse.ny);
    const src = pulse.frames[Math.min(pulse.frames.length - 1, Math.max(0, frame))];
    for (let i = 0; i < src.length; i++) {
      const s = Math.pow(Math.min(1, src[i]), 0.5);
      const k = Math.round(s * 255);
      img.data[i * 4] = fieldLut[k * 4]; img.data[i * 4 + 1] = fieldLut[k * 4 + 1]; img.data[i * 4 + 2] = fieldLut[k * 4 + 2];
      img.data[i * 4 + 3] = Math.min(255, s * 420);
    }
    ctx.putImageData(img, 0, 0);
  }, [pulse, frame]);
  return <canvas ref={ref} width={pulse.nx} height={pulse.ny} style={{ position: "absolute", left: pulse.u[0], top: -pulse.v[1], width: pulse.u[1] - pulse.u[0], height: pulse.v[1] - pulse.v[0], transform: "scaleY(-1)", mixBlendMode: "screen" }} />;
}
