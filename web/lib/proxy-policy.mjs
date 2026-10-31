// Same-origin API proxy policy. Only the routes listed below pass.
const ids = "[a-zA-Z0-9_-]+";
// The processed files of a simulation job (the API's simulation job files).
const simFile = "(?:(?:s11|s11_as_built|zin|pattern|field|pulse|current|result|summary|meta)\\.json|(?:field_xy|field_xz|pulse|current)\\.bin|model\\.png)";
const routes = [
  ["GET", /^health$/],
  ["GET", /^bootstrap$/],
  ["GET", /^runs$/],
  ["GET", /^projects$/],
  ["POST", /^projects$/],
  ["GET", new RegExp(`^projects/${ids}$`)],
  ["POST", new RegExp(`^projects/${ids}/versions$`)],
  ["GET", new RegExp(`^versions/${ids}$`)],
  ["POST", new RegExp(`^versions/${ids}/reviews$`)],
  ["GET", new RegExp(`^reviews/${ids}$`)],
  ["GET", new RegExp(`^reviews/${ids}/(stream|events|findings|assumptions|report|sims)$`)],
  ["GET", new RegExp(`^sim/${ids}/files/${simFile}$`)],
  // power, heat and signal runs: the run itself, and the raster of one of its overlays
  ["GET", new RegExp(`^reviews/${ids}/sims/${ids}$`)],
  ["GET", new RegExp(`^reviews/${ids}/sims/${ids}/overlay/${ids}(?:\\.${ids})*$`)],     // an overlay id may carry a layer name such as vdrop_F.Cu, never ".."
  ["GET", new RegExp(`^reviews/${ids}/report/revisions$`)],
  ["GET", new RegExp(`^reviews/${ids}/report/${ids}\\.(html|pdf|json)$`)],
  ["POST", new RegExp(`^reviews/${ids}/(cancel|threads|restart)$`)],
  ["POST", new RegExp(`^threads/${ids}/(messages|cancel)$`)],
  ["GET", new RegExp(`^threads/${ids}/(stream|events)$`)],
  ["POST", new RegExp(`^requests/${ids}/respond$`)],
  ["GET", new RegExp(`^findings/${ids}/history$`)],
  ["PATCH", new RegExp(`^findings/${ids}$`)],
  ["GET", /^settings\/keys$/],
  ["GET", /^compare$/],
  ["GET", /^settings\/keys$/],
  ["PUT", /^settings\/keys\/[A-Z_]+$/],
  ["DELETE", /^settings\/keys\/[A-Z_]+$/],
  ["GET", new RegExp(`^artifacts/${ids}$`)],
];
export function allowedRoute(method, path) {
  return routes.some(
    ([verb, pattern]) => method === verb && pattern.test(path),
  );
}
export function trustedRequest(host, origin, method) {
  if (!["127.0.0.1:3118", "localhost:3118"].includes(host ?? "")) return false;
  if (!["GET", "HEAD"].includes(method)) return origin === `http://${host}`;
  return !origin || origin === `http://${host}`;
}

/** Version artifacts (board.json, scene SVGs, the GLB) and processed simulation files are the only responses a browser may keep for a short time. The backend decides how long (finished jobs only). */
export function isArtifactRoute(method, path) {
  return method === "GET" && (new RegExp(`^artifacts/${ids}$`).test(path) || new RegExp(`^sim/${ids}/files/${simFile}$`).test(path));
}
