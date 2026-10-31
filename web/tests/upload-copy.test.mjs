import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The depth notes on the Upload page must quote the wall-clock budgets in the spec's default budgets table.
test("upload depth notes match the wall-clock budgets", () => {
  const spec = readFileSync(new URL("../../docs/specs/2026-10-31-trustpcb-spec.md", import.meta.url), "utf8");
  const row = spec.split("\n").find((l) => l.startsWith("| Wall clock"));
  assert.ok(row, "spec has a wall clock row");
  const [quick, standard, deep] = row.split("|").slice(2, 5).map((c) => parseInt(c.trim(), 10));
  const src = readFileSync(new URL("../components/pages/Upload.tsx", import.meta.url), "utf8");
  assert.match(src, new RegExp(`id: "quick".*About ${quick} min`));
  assert.match(src, new RegExp(`id: "standard".*Up to ${standard} min`));
  assert.match(src, new RegExp(`id: "deep".*Up to ${deep} min`));
});
