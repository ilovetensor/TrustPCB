import { test } from "node:test";
import assert from "node:assert/strict";
import { allowedRoute, trustedRequest } from "../lib/proxy-policy.mjs";
test("proxy only accepts contracted paths", () => {
  assert.ok(allowedRoute("GET", "reviews/r-1/stream"));
  assert.ok(allowedRoute("POST", "versions/v_1/reviews"));
  assert.ok(allowedRoute("PATCH", "findings/F-007"));
  assert.ok(allowedRoute("GET", "reviews/r1/report/r3.pdf"));
  for (const path of [
    "../health",
    "http://evil.test",
    "reviews/a/stream/extra",
    "projects//versions",
    "reviews/a/report/r3.exe",
  ])
    assert.equal(allowedRoute("GET", path), false);
  assert.equal(allowedRoute("POST", "reviews/a/stream"), false);
  assert.equal(allowedRoute("DELETE", "projects/a"), false);
});
test("proxy serves a review's simulation list and only the contracted job files", () => {
  assert.ok(allowedRoute("GET", "reviews/r-1/sims"));
  assert.ok(allowedRoute("GET", "sim/ab12cd/files/field_xy.bin"));
  assert.ok(allowedRoute("GET", "sim/ab12cd/files/s11_as_built.json"));
  assert.ok(allowedRoute("GET", "sim/ab12cd/files/model.png"));
  for (const path of ["sim/ab/files/scenario.json", "sim/ab/files/progress.jsonl", "sim/ab/files/../s11.json", "sim/ab/files/s11.json/x", "sim/ab/files/pattern.bin"]) assert.equal(allowedRoute("GET", path), false, path);
  assert.equal(allowedRoute("POST", "reviews/r-1/sims"), false);
});
test("proxy serves one board simulation run, the raster of one overlay, and the key status, read only", () => {
  assert.ok(allowedRoute("GET", "reviews/r-1/sims/fx-p1"));
  assert.ok(allowedRoute("GET", "reviews/r-1/sims/ab12/overlay/vdrop_F.Cu"));
  assert.ok(allowedRoute("GET", "reviews/r-1/sims/ab12/overlay/temp"));
  for (const id of ["drop:F.Cu", "current:B.Cu", "gndrise:F.Cu", "z0_deviation_F.Cu", "return_detour", "temp_board"]) assert.ok(allowedRoute("GET", `reviews/r-1/sims/ab12/overlay/${id}`), id);
  for (const id of ["a:b:c", ":x", "drop:", "a:/b"]) assert.equal(allowedRoute("GET", `reviews/r-1/sims/ab12/overlay/${id}`), false, id);
  assert.ok(allowedRoute("GET", "settings/keys"));
  for (const path of ["reviews/r-1/sims/ab/overlay", "reviews/r-1/sims/ab/overlay/a/b", "reviews/r-1/sims/../x", "reviews/r-1/sims/ab/overlay/vdrop..Cu", "reviews/r-1/sims/ab/overlay/.hidden", "settings/keys/x", "settings/secrets"]) assert.equal(allowedRoute("GET", path), false, path);
  for (const method of ["POST", "PUT", "DELETE", "PATCH"]) assert.equal(allowedRoute(method, "reviews/r-1/sims/ab/overlay/a"), false);
  assert.equal(allowedRoute("PUT", "settings/keys"), false);
});
test("proxy requires exact loopback host and mutation origin", () => {
  assert.ok(trustedRequest("127.0.0.1:3118", "http://127.0.0.1:3118", "POST"));
  assert.ok(trustedRequest("127.0.0.1:3118", "http://127.0.0.1:3118", "PATCH"));
  assert.ok(trustedRequest("localhost:3118", null, "GET"));
  for (const host of ["evil.test:3118", "127.0.0.1.evil:3118", "localhost:4000"])
    assert.equal(trustedRequest(host, `http://${host}`, "POST"), false);
  assert.equal(trustedRequest("127.0.0.1:3118", null, "POST"), false);
  assert.equal(trustedRequest("127.0.0.1:3118", "http://evil.test", "GET"), false);
});
import { isArtifactRoute } from "../lib/proxy-policy.mjs";
test("only artifact reads may be cached", () => {
  assert.ok(isArtifactRoute("GET", "artifacts/ab12cd"));
  assert.equal(isArtifactRoute("POST", "artifacts/ab12cd"), false);
  assert.equal(isArtifactRoute("GET", "reviews/a/report/1.html"), false);
  assert.equal(isArtifactRoute("GET", "artifacts/ab/../x"), false);
  assert.ok(isArtifactRoute("GET", "sim/ab12/files/pulse.bin"));
  assert.equal(isArtifactRoute("GET", "reviews/r/sims"), false);
});
test("proxy passes the key settings routes and nothing near them", () => {
  assert.ok(allowedRoute("GET", "settings/keys"));
  assert.ok(allowedRoute("PUT", "settings/keys/MOUSER_API_KEY"));
  assert.ok(allowedRoute("DELETE", "settings/keys/DIGIKEY_CLIENT_ID"));
  for (const [verb, path] of [["GET", "settings/keys/MOUSER_API_KEY"], ["POST", "settings/keys"], ["PUT", "settings/keys"], ["PUT", "settings/keys/../x"], ["DELETE", "settings/keys"], ["PUT", "settings/keys/mouser"], ["GET", "settings"]])
    assert.equal(allowedRoute(verb, path), false, `${verb} ${path}`);
});
