"use client";
import { useEffect, useState } from "react";

/**
 * Run a loader when `key` changes. `data` is undefined while loading, null when the loader found nothing,
 * and `error` is set when it threw. A null loader means "nothing to load" and leaves data null.
 */
export function useAsync<T>(load: (() => Promise<T>) | null, key: string): { data: T | null | undefined; error: boolean } {
  const [state, setState] = useState<{ key: string; data: T | null | undefined; error: boolean }>({ key, data: undefined, error: false });
  useEffect(() => {
    if (!load) { setState({ key, data: null, error: false }); return; }
    let live = true;
    setState({ key, data: undefined, error: false });
    load().then((data) => { if (live) setState({ key, data, error: false }); }).catch(() => { if (live) setState({ key, data: null, error: true }); });
    return () => { live = false; };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps
  return state.key === key ? { data: state.data, error: state.error } : { data: undefined, error: false };
}
