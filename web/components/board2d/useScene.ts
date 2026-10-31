"use client";
import { useEffect, useState } from "react";
import type { SceneInfo } from "../../lib/api";
import { sanitizeSvgInner } from "../../lib/svg-sanitize.mjs";

/** Inner markup of the scene SVG layers, ready to place in the board's own <g>. */
export interface SceneMarkup { silkTop?: string; silkBottom?: string; refsTop?: string; refsBottom?: string; courtyardTop?: string }

const cache = new Map<string, Promise<string | null>>();

/** Fetch one layer SVG and return what sits inside the root element, rebuilt from an allow-list of drawing elements and attributes. */
function inner(url: string): Promise<string | null> {
  let p = cache.get(url);
  if (!p) {
    p = fetch(url).then((r) => (r.ok ? r.text() : null)).then((t) => (t ? sanitizeSvgInner(t) : null)).catch(() => null);
    cache.set(url, p);
  }
  return p;
}

export function useScene(scene: SceneInfo | null | undefined): SceneMarkup | null {
  const [markup, setMarkup] = useState<SceneMarkup | null>(null);
  useEffect(() => {
    if (!scene) { setMarkup(null); return; }
    let live = true;
    const u = scene.urls;
    void Promise.all([
      u["F.SilkS"] ? inner(u["F.SilkS"]) : null, u["B.SilkS"] ? inner(u["B.SilkS"]) : null,
      u["F.Refs"] ? inner(u["F.Refs"]) : null, u["B.Refs"] ? inner(u["B.Refs"]) : null, u["F.CrtYd"] ? inner(u["F.CrtYd"]) : null,
    ]).then(([silkTop, silkBottom, refsTop, refsBottom, courtyardTop]) => {
      if (live) setMarkup({ silkTop: silkTop ?? undefined, silkBottom: silkBottom ?? undefined, refsTop: refsTop ?? undefined, refsBottom: refsBottom ?? undefined, courtyardTop: courtyardTop ?? undefined });
    });
    return () => { live = false; };
  }, [scene]);
  return markup;
}
