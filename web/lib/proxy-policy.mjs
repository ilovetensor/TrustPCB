// Same-origin API proxy policy. Only the contracted routes (spec section 18) pass.
const ids = "[a-zA-Z0-9_-]+";
const routes = [
  ["GET", /^health$/],
  ["GET", /^bootstrap$/],
  ["GET", /^projects$/],
  ["POST", /^projects$/],
  ["GET", new RegExp(`^projects/${ids}$`)],
  ["POST", new RegExp(`^projects/${ids}/versions$`)],
  ["GET", new RegExp(`^versions/${ids}$`)],
  ["POST", new RegExp(`^versions/${ids}/reviews$`)],
  ["GET", new RegExp(`^reviews/${ids}$`)],
  ["GET", new RegExp(`^reviews/${ids}/(stream|events|findings|assumptions|report)$`)],
  ["GET", new RegExp(`^reviews/${ids}/report/revisions$`)],
  ["GET", new RegExp(`^reviews/${ids}/report/${ids}\\.(html|pdf|json)$`)],
  ["POST", new RegExp(`^reviews/${ids}/(cancel|threads)$`)],
  ["POST", new RegExp(`^threads/${ids}/(messages|cancel)$`)],
  ["GET", new RegExp(`^threads/${ids}/(stream|events)$`)],
  ["POST", new RegExp(`^requests/${ids}/respond$`)],
  ["GET", new RegExp(`^findings/${ids}/history$`)],
  ["PATCH", new RegExp(`^findings/${ids}$`)],
  ["GET", /^compare$/],
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

/** Version artifacts (board.json, scene SVGs, the GLB) are the only responses a browser may keep for a short time. */
export function isArtifactRoute(method, path) {
  return method === "GET" && new RegExp(`^artifacts/${ids}$`).test(path);
}
