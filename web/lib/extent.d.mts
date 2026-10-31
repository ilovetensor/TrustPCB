export type ExtentId = "layout" | "antenna" | "power" | "thermal" | "signal" | "datasheets";
export interface ExtentOption { id: ExtentId; label: string; icon: string; line: string; cost: [number, number] | null; always: boolean; available: boolean; reason: string | null }
export const EXTENT_IDS: ExtentId[];
export const EXTENT_INFO: Record<ExtentId, { label: string; icon: string; line: string; cost: [number, number] | null; always?: boolean }>;
export function datasheetKeysSet(status: Record<string, { set: boolean }> | null | undefined): boolean;
export function extentOptions(o?: {
  detection?: { wireless?: boolean; antenna?: { type?: string } | null } | null; keys?: Record<string, { set: boolean }> | null;
  server?: Partial<Record<ExtentId, { available: boolean; reason?: string }>> | null; depth?: string;
}): ExtentOption[];
export function defaultExtent(options: ExtentOption[]): ExtentId[];
export function everythingExtent(options: ExtentOption[]): ExtentId[];
export function layoutOnly(): ExtentId[];
export function cleanExtent(selected: ExtentId[], options: ExtentOption[]): ExtentId[];
export function sameExtent(a: ExtentId[], b: ExtentId[]): boolean;
export function extentCost(selected: ExtentId[]): [number, number] | null;
export function costText(c: [number, number] | null): string;
export function costLine(c: [number, number] | null): string;
