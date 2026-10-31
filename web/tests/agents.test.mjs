import { test } from "node:test";
import assert from "node:assert/strict";
import { agentFor, agentLabel, runnerOfProfile, NEUTRAL } from "../lib/agents.mjs";

test("muse_api is Meta Muse with a brand and a logo path", () => {
  const a = agentFor("muse_api");
  assert.equal(a.brand, "meta");
  assert.equal(a.name, "Meta Muse");
  assert.ok(a.logo && a.logo.endsWith(".svg"));
});
test("any other runner, or none, is the neutral agent with no brand", () => {
  for (const r of ["scripted", "dev_runner", "other_runner", "", "MUSE_API", "muse_api ", null, undefined, 7, {}, "__proto__", "constructor", "toString"]) {
    const a = agentFor(r);
    assert.equal(a, NEUTRAL, String(r));
    assert.equal(a.brand, null);
    assert.equal(a.logo, null);
  }
});
test("labels are sentence case and name the real writer", () => {
  assert.equal(agentLabel("muse_api"), "Answered by Meta Muse");
  assert.equal(agentLabel("muse_api", "wrote"), "Written by Meta Muse");
  assert.equal(agentLabel("muse_api", "working"), "Meta Muse is working");
  assert.equal(agentLabel("scripted"), "Answered by the agent");
  assert.equal(agentLabel(undefined, "working"), "The agent is working");
  assert.ok(!/meta/i.test(agentLabel("anything_else", "wrote")));
});
test("runnerOfProfile reads the stored profile and tolerates old rows", () => {
  assert.equal(runnerOfProfile({ runner: "muse_api", name: "muse_api" }), "muse_api");
  assert.equal(runnerOfProfile({ name: "scripted" }), "scripted");
  assert.equal(runnerOfProfile({ runner: "", name: "" }), null);
  for (const p of [null, undefined, "muse_api", 3, {}]) assert.equal(runnerOfProfile(p), null);
  assert.equal(agentFor(runnerOfProfile({ runner: "muse_api" })).brand, "meta");
  assert.equal(agentFor(runnerOfProfile(null)).brand, null);
});
