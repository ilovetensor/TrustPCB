import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { rfLabExplanation, sentence, simulationsNote } from "../lib/rf-note.mjs";

test("the server's reason is shown as a sentence", () => {
  assert.equal(sentence("no wireless part or antenna was found on this board"), "No wireless part or antenna was found on this board.");
  assert.equal(sentence("The RF check did not run."), "The RF check did not run.");
  assert.equal(sentence(null), "");
});

test("the Simulations note gives the reason as a sentence and does not repeat the Not tested label", () => {
  assert.equal(simulationsNote({ status: "not_tested", reason: "no wireless part or antenna was found on this board" }), "No wireless part or antenna was found on this board.");
  assert.equal(simulationsNote({ status: "not_tested", reason: null }), "No simulation was run for this review.");
  assert.equal(simulationsNote({ status: "available", reason: null }), null);
  assert.equal(simulationsNote(null), null);
});

test("the RF lab does not promise a simulation to a board that gave a reason for having none", () => {
  const noRadio = rfLabExplanation({ reason: "no wireless part or antenna was found on this board" });
  assert.equal(noRadio.why, "No wireless part or antenna was found on this board.");
  assert.equal(noRadio.hint, "", "no 'Standard and Deep reviews simulate the antenna' line next to a no-radio reason");
  const quick = rfLabExplanation({ reason: null });
  assert.equal(quick.why, "");
  assert.match(quick.hint, /Quick reviews run no simulation/);
  assert.match(noRadio.lead, /Nothing in this review was simulated/);
});

test("both views read their wording from the one module", () => {
  const ws = readFileSync(new URL("../components/workspace/Workspace.tsx", import.meta.url), "utf8");
  const lab = readFileSync(new URL("../components/rf/RfLabPage.tsx", import.meta.url), "utf8");
  assert.match(ws, /simulationsNote\(meta\?\.rf\)/);
  assert.match(lab, /rfLabExplanation\(meta\?\.rf\)/);
});
