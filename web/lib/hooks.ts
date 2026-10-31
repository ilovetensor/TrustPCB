"use client";
import { useEffect, useState } from "react";

export function useMedia(query: string, initial = false) {
  const [m, setM] = useState(initial);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setM(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return m;
}
export const useNarrow = () => useMedia("(max-width: 1099px)");

/** Is the visitor asking for less motion? Read once, safe on the server (false there). */
export const reducedMotion = () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
export const useReducedMotion = () => useMedia("(prefers-reduced-motion: reduce)");
/** The phase a still frame of a looping wave is held at, so the field structure reads without moving. */
export const STILL_PHASE = 1.15;
