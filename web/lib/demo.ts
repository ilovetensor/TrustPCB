"use client";
// The read-only demo flag. The backend sets `demo_readonly` in /api/bootstrap when TRUSTPCB_DEMO_READONLY=1; in that mode every change is refused with 403,
// so the interface hides upload and start and says why. Until the answer arrives, or when it cannot be read, the app behaves as a normal local app.

import { useEffect, useState } from "react";
import { apiGet } from "./live";

export interface AppEnv { demo: boolean; repoUrl: string | null; kicad: { available: boolean; version: string | null; reason: string | null } | null }
const OFF: AppEnv = { demo: false, repoUrl: null, kicad: null };
let pending: Promise<AppEnv> | null = null;

function loadEnv(): Promise<AppEnv> {
  if (process.env.NEXT_PUBLIC_TRUSTPCB_FIXTURES === "1") return Promise.resolve(OFF);
  pending ??= apiGet<{ demo_readonly?: boolean; repo_url?: string | null; kicad?: AppEnv["kicad"] }>("bootstrap")
    .then((b) => ({ demo: !!b.demo_readonly, repoUrl: b.repo_url ?? null, kicad: b.kicad ?? null }))
    .catch(() => { pending = null; return OFF; });
  return pending;
}

export function useAppEnv(): AppEnv {
  const [env, setEnv] = useState<AppEnv>(OFF);
  useEffect(() => { let live = true; void loadEnv().then((e) => { if (live) setEnv(e); }); return () => { live = false; }; }, []);
  return env;
}
