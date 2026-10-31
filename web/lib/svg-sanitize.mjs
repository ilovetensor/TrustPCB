// Allow-list sanitiser for the server-made board layer SVGs that are placed into the page with dangerouslySetInnerHTML.
// The text is parsed with DOMParser, the tree is walked, and a fresh string is built from only the elements and
// attributes named below. Everything else (script, style, foreignObject, animation, links, images, event handlers,
// external references, namespaced content) is dropped together with its children. Without a DOM (server rendering)
// there is nothing to parse with, so it returns null and the layer is simply not drawn.

const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_CHARS = 12_000_000;

export const ALLOWED_ELEMENTS = new Set(["g", "path", "polyline", "polygon", "line", "circle", "ellipse", "rect", "text", "tspan"]);

export const ALLOWED_ATTRIBUTES = new Set([
  "d", "points", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "dx", "dy", "transform", "class",
  "fill", "fill-opacity", "fill-rule", "opacity", "stroke", "stroke-width", "stroke-opacity", "stroke-linecap", "stroke-linejoin",
  "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit", "paint-order", "vector-effect", "visibility", "display",
  "font-family", "font-size", "font-weight", "font-style", "letter-spacing", "text-anchor", "dominant-baseline", "alignment-baseline",
]);

/** data-* names are allowed (the page's CSS selects on data-role); the rest of the name must be plain. */
const DATA_ATTR = /^data-[a-z0-9_.-]+$/;
/** A value that can pull in something from outside, run script, or smuggle markup. Such an attribute is dropped. */
const BAD_VALUE = /url\s*\(|javascript\s*:|vbscript\s*:|data\s*:|expression\s*\(|@import|&#|<|>|\\/i;

const escText = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escAttr = (s) => escText(s).replace(/"/g, "&quot;");

function emit(node, out) {
  for (const child of node.childNodes) {
    if (child.nodeType === 3 || child.nodeType === 4) {
      // text only means something inside text elements; elsewhere it is whitespace between shapes
      if (node.localName === "text" || node.localName === "tspan") out.push(escText(child.nodeValue ?? ""));
      continue;
    }
    if (child.nodeType !== 1) continue; // comments, processing instructions
    if (child.namespaceURI !== SVG_NS || !ALLOWED_ELEMENTS.has(child.localName)) continue;
    const name = child.localName;
    out.push("<", name);
    for (const a of child.attributes) {
      if (a.namespaceURI || a.prefix) continue;
      const n = a.localName;
      const data = DATA_ATTR.test(n);
      if (!data && !ALLOWED_ATTRIBUTES.has(n)) continue;
      if (!data && BAD_VALUE.test(a.value)) continue; // data-* values are inert text, they are only escaped
      out.push(" ", n, '="', escAttr(a.value), '"');
    }
    if (!child.firstChild) { out.push("/>"); continue; }
    out.push(">");
    emit(child, out);
    out.push("</", name, ">");
  }
}

/**
 * Parse an SVG document and return the sanitised markup that sits inside its root element, or null when the text is
 * not a well formed SVG, is too large, declares entities, or no DOMParser exists.
 * `parser` is only for tests; it defaults to the platform DOMParser.
 */
export function sanitizeSvgInner(text, parser) {
  if (typeof text !== "string" || !text || text.length > MAX_CHARS) return null;
  const P = parser ?? (typeof DOMParser === "undefined" ? null : new DOMParser());
  if (!P) return null;
  // entity declarations can expand without bound and are never produced by the server
  if (/<!\s*(ENTITY|DOCTYPE)/i.test(text)) return null;
  let doc;
  try { doc = P.parseFromString(text, "image/svg+xml"); } catch { return null; }
  const root = doc.documentElement;
  if (!root || root.localName !== "svg" || root.namespaceURI !== SVG_NS) return null;
  if (doc.getElementsByTagName("parsererror").length) return null;
  const out = [];
  emit(root, out);
  return out.join("");
}
