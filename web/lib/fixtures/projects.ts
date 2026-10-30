// DEV-ONLY FIXTURE. Project list, versions and a version comparison for the dashboards.
import type { ProjectSummary, Severity } from "../types";

const c = (critical: number, high: number, medium: number, low: number, info: number): Record<Severity, number> => ({ critical, high, medium, low, info });

export const PROJECTS: ProjectSummary[] = [
  {
    id: "p-nrf52840", name: "nRF52840 RF IoT Node", kind: "BLE node, printed meander antenna", updated_at: "2026-10-31T09:01:00Z",
    versions: 2, latest_version: "v2", latest_review_id: "r-0042", counts: c(0, 1, 2, 3, 2), rf: "fail", state: "complete", trend: [9, 5], fixture: true,
  },
  {
    id: "p-patch2g5", name: "2.5 GHz Patch Antenna", kind: "Patch antenna test board", updated_at: "2026-10-30T18:20:00Z",
    versions: 1, latest_version: "v1", latest_review_id: "r-0040", counts: c(0, 0, 1, 1, 1), rf: "pass", state: "complete", trend: [3], fixture: true,
  },
  {
    id: "p-usbhub", name: "USB-C Hub Rev B", kind: "No wireless part", updated_at: "2026-10-29T12:00:00Z",
    versions: 3, latest_version: "v3", latest_review_id: "r-0037", counts: c(1, 2, 4, 5, 1), rf: "n/a", state: "complete_with_limits", trend: [14, 11, 13], fixture: true,
  },
  {
    id: "p-sensorpod", name: "Sensor Pod", kind: "Module antenna", updated_at: "2026-10-28T08:40:00Z",
    versions: 1, latest_version: "v1", latest_review_id: "r-0033", counts: c(0, 1, 0, 2, 0), rf: "not_tested", state: "agent_stopped", trend: [3], fixture: true,
  },
];

export interface VersionRow { id: string; label: string; review_id: string; at: string; counts: Record<Severity, number>; f0_ghz: number | null; note: string }

export const VERSIONS: Record<string, VersionRow[]> = {
  "p-nrf52840": [
    { id: "ver-1", label: "v1", review_id: "r-0031", at: "2026-10-30T21:10:00Z", counts: c(0, 2, 3, 3, 1), f0_ghz: 2.285, note: "First upload. Antenna 155 MHz low." },
    { id: "ver-2", label: "v2", review_id: "r-0042", at: "2026-10-31T09:01:00Z", counts: c(0, 1, 2, 3, 2), f0_ghz: 2.312, note: "Pour pulled back. Antenna still below band." },
  ],
  "p-patch2g5": [
    { id: "ver-p1", label: "v1", review_id: "r-0040", at: "2026-10-30T18:20:00Z", counts: c(0, 0, 1, 1, 1), f0_ghz: 2.4, note: "Resonates inside the band." },
  ],
  "p-usbhub": [
    { id: "ver-u1", label: "v1", review_id: "r-0030", at: "2026-10-27T08:00:00Z", counts: c(1, 3, 5, 4, 1), f0_ghz: null, note: "" },
    { id: "ver-u2", label: "v2", review_id: "r-0034", at: "2026-10-28T08:00:00Z", counts: c(1, 2, 4, 4, 0), f0_ghz: null, note: "" },
    { id: "ver-u3", label: "v3", review_id: "r-0037", at: "2026-10-29T12:00:00Z", counts: c(1, 2, 4, 5, 1), f0_ghz: null, note: "" },
  ],
  "p-sensorpod": [
    { id: "ver-s1", label: "v1", review_id: "r-0033", at: "2026-10-28T08:40:00Z", counts: c(0, 1, 0, 2, 0), f0_ghz: null, note: "Agent stopped at the budget." },
  ],
};

/** Finding changes between v1 and v2 of the nRF board. */
export const COMPARE_NRF = [
  { id: "F-001", change: "still_present" as const, title: "Antenna resonates below the band", a: "2.285 GHz", b: "2.312 GHz", sevA: "high" as Severity, sevB: "high" as Severity },
  { id: "F-002", change: "changed" as const, title: "Antenna feed close to pour boundary", a: "0.10 mm", b: "0.25 mm", sevA: "high" as Severity, sevB: "medium" as Severity },
  { id: "F-007", change: "still_present" as const, title: "Power track takes a long detour", a: "30.7 mm", b: "30.7 mm", sevA: "medium" as Severity, sevB: "medium" as Severity },
  { id: "F-009", change: "fixed" as const, title: "Ground pour on the back layer was missing", a: "none", b: "present", sevA: "medium" as Severity, sevB: "info" as Severity },
  { id: "F-008", change: "new" as const, title: "No ground stitching vias near the feed", a: "-", b: "0 vias", sevA: "info" as Severity, sevB: "low" as Severity },
];
