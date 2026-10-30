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
test("proxy requires exact loopback host and mutation origin", () => {
  assert.ok(trustedRequest("127.0.0.1:3118", "http://127.0.0.1:3118", "POST"));
  assert.ok(trustedRequest("127.0.0.1:3118", "http://127.0.0.1:3118", "PATCH"));
  assert.ok(trustedRequest("localhost:3118", null, "GET"));
  for (const host of ["evil.test:3118", "127.0.0.1.evil:3118", "localhost:4000"])
    assert.equal(trustedRequest(host, `http://${host}`, "POST"), false);
  assert.equal(trustedRequest("127.0.0.1:3118", null, "POST"), false);
  assert.equal(trustedRequest("127.0.0.1:3118", "http://evil.test", "GET"), false);
});
