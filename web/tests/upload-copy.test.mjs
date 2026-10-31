import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The depth notes on the Upload page must quote the wall-clock budgets of the server's CONFIGS (minutes).
test("upload depth notes match the wall-clock budgets", () => {
  const budgets = readFileSync(new URL("../../server/trustpcb/review/budgets.py", import.meta.url), "utf8");
  const minutes = Object.fromEntries([...budgets.matchAll(/BudgetConfig\('(quick|standard|deep)', (\d+) \* 60,/g)].map((m) => [m[1], Number(m[2])]));
  const { quick, standard, deep } = minutes;
  assert.ok(quick && standard && deep, "budgets.py CONFIGS has a wall clock for each depth");
  const src = readFileSync(new URL("../components/pages/Upload.tsx", import.meta.url), "utf8");
  assert.match(src, new RegExp(`id: "quick".*About ${quick} min`));
  assert.match(src, new RegExp(`id: "standard".*Up to ${standard} min`));
  assert.match(src, new RegExp(`id: "deep".*Up to ${deep} min`));
});
