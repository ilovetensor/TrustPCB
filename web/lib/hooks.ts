"use client";
import { useEffect, useState, type KeyboardEvent } from "react";

function useMedia(query: string, initial = false) {
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

/** Props for one tab in a tab list: only the selected tab is in the tab order, the arrow keys move between the rest (see tablistKeys). */
export const tabProps = (selected: boolean) => ({ role: "tab" as const, "aria-selected": selected, tabIndex: selected ? 0 : -1 });

/**
 * onKeyDown for the element that holds role="tab" buttons. Left and right (up and down when vertical) move to the
 * previous or next enabled tab, Home and End to the first and last, and the tab that receives focus is selected.
 */
export const tablistKeys = (vertical = false) => (e: KeyboardEvent<HTMLElement>) => {
  const prev = vertical ? "ArrowUp" : "ArrowLeft", next = vertical ? "ArrowDown" : "ArrowRight";
  if (![prev, next, "Home", "End"].includes(e.key)) return;
  const tabs = [...e.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]:not([disabled])')];
  const at = tabs.indexOf((e.target as HTMLElement).closest<HTMLElement>('[role="tab"]') as HTMLElement);
  if (!tabs.length || at < 0) return;
  const to = e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : (at + (e.key === next ? 1 : -1) + tabs.length) % tabs.length;
  e.preventDefault();
  tabs[to].focus();
  tabs[to].click();
};
