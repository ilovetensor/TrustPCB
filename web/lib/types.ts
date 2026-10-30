// Domain types. Mirrors docs/specs/contracts.md (snake_case, millimetres, KiCad board coords: x right, y down).

export type Pt = [number, number];
export type Ring = Pt[];
export type BBox = [number, number, number, number];

export type LayerKind = "copper" | "silk" | "mask" | "paste" | "fab" | "edge" | "other";
export interface BoardLayer { name: string; kind: LayerKind; index: number }
export interface Footprint {
  id: string; ref: string; value: string; lib_id: string;
  x: number; y: number; rotation: number; side: "top" | "bottom";
  bbox: BBox; courtyard: Ring | null; pad_ids: string[]; dnp: boolean;
}
export interface Pad {
  id: string; ref: string; number: string; net: string | null;
  x: number; y: number; w: number; h: number; rotation: number;
  shape: string; layers: string[]; drill: number | null; ring: Ring;
}
export interface Track {
  id: string; net: string | null; layer: string; width: number;
  start: Pt; end: Pt; mid: Pt | null; length: number;
}
export interface Via {
  id: string; net: string | null; x: number; y: number;
  diameter: number; drill: number; layers: string[];
}
export interface Zone {
  id: string; net: string | null; name?: string; layers: string[];
  filled: Record<string, Ring[]>; is_keepout: false;
}
export interface Keepout { id: string; name?: string; layers: string[]; ring: Ring; rules: Record<string, unknown> }
export type SilkItem =
  | { layer: string; kind: "line"; pts: Pt[]; w: number }
  | { layer: string; kind: "poly"; pts: Pt[]; w: number; closed?: boolean }
  | { layer: string; kind: "circle"; c: Pt; r: number; w: number }
  | { layer: string; kind: "arc"; pts: Pt[]; w: number }
  | { layer: string; kind: "text"; text: string; x: number; y: number; rot: number; size: number };

export interface Board {
  version_id: string; title: string; outline: Ring[]; bbox: BBox;
  layers: BoardLayer[];
  stackup: { source: "board" | "default"; assumed: boolean; layers: unknown[] };
  nets: { id: string; name: string; net_class: string }[];
  footprints: Footprint[]; pads: Pad[]; tracks: Track[]; vias: Via[];
  zones: Zone[]; keepouts: Keepout[];
  /** Drawing-only extras while the renderer is a dev fixture. */
  silk?: SilkItem[];
}

export interface Geometry {
  kind: "point" | "polyline" | "polygon" | "group";
  points?: Pt[];
  rings?: Ring[];
  parts?: Geometry[];
  bbox: BBox;
  layer?: string;
  side?: "top" | "bottom";
  radius?: number;
}

export type MarkType = "pin" | "object" | "path" | "region" | "link" | "sketch";
export interface Mark {
  type: MarkType;
  targets: string[];
  label?: string;
  value?: { value: number; unit: string };
  role?: string;
  resolved?: Geometry[];
}

export type Severity = "critical" | "high" | "medium" | "low" | "info";
export type EvidenceClass = "measured" | "simulated" | "opinion" | "not_tested";
export type FindingStatus = "open" | "dismissed" | "fixed" | "needs_recheck" | "superseded";

export interface Finding {
  id: string; fingerprint: string; title: string;
  severity: Severity; class: EvidenceClass; status: FindingStatus;
  what: string; why_it_matters: string; recommendation: string;
  evidence_ids: string[]; marks: Mark[];
  assumptions: string[]; not_tested: string[];
  origin?: "review" | "followup";
  /** Present for version comparison. */
  change?: "new" | "fixed" | "still_present" | "changed";
}

export interface Evidence {
  id: string; type: "drc" | "analyzer" | "measurement" | "simulation" | "render";
  source_tool: string; summary: string;
  values: Record<string, { value: number; unit: string }>;
  trust?: "ok" | "failed" | "unchecked";
}

export interface EventEnvelope {
  id: string; sequence: number; project_id: string; chat_id: string; turn_id: string;
  type: string; payload: Record<string, any>; created_at: string;
}

export type PhaseId = 0 | 1 | 2 | 3 | 4 | 5 | 6;

export interface ProjectSummary {
  id: string; name: string; kind: string; updated_at: string;
  versions: number; latest_version: string; latest_review_id: string;
  counts: Record<Severity, number>;
  rf: "pass" | "fail" | "not_tested" | "n/a";
  state: "complete" | "complete_with_limits" | "running" | "agent_stopped" | "queued";
  trend: number[];
  fixture?: boolean;
}
