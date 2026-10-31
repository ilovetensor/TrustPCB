export const ALLOWED_ELEMENTS: Set<string>;
export const ALLOWED_ATTRIBUTES: Set<string>;
export function sanitizeSvgInner(text: string, parser?: { parseFromString(s: string, type: string): Document }): string | null;
