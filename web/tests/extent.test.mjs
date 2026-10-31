import { test } from "node:test";
import assert from "node:assert/strict";
import { cleanExtent, costLine, costText, datasheetKeysSet, defaultExtent, everythingExtent, extentCost, extentOptions, layoutOnly, sameExtent } from "../lib/extent.mjs";

const radio = { wireless: true, antenna: { type: "meander", shape: "meander" } };
const noRadio = { wireless: false };
const keys = { DIGIKEY_CLIENT_ID: { set: true }, DIGIKEY_CLIENT_SECRET: { set: true }, MOUSER_API_KEY: { set: false } };
const by = (o) => Object.fromEntries(o.map((x) => [x.id, x]));

test("layout is always there; before the upload nothing else can be chosen and the reason says so", () => {
  const o = by(extentOptions({}));
  assert.equal(o.layout.available, true); assert.equal(o.layout.always, true);
  for (const id of ["antenna", "power", "thermal", "signal", "datasheets"]) assert.equal(o[id].available, false, id);
  assert.match(o.antenna.reason, /after the upload/i);
});

test("a board with a printed antenna can run the antenna check, one with none says why not", () => {
  assert.equal(by(extentOptions({ detection: radio }))["antenna"].available, true);
  const n = by(extentOptions({ detection: noRadio }))["antenna"];
  assert.equal(n.available, false); assert.match(n.reason, /no printed antenna/i);
  const chip = by(extentOptions({ detection: { wireless: true, antenna: { type: "chip" } } }))["antenna"];
  assert.equal(chip.available, false); assert.match(chip.reason, /chip antenna/i);
  assert.match(by(extentOptions({ detection: { wireless: true, antenna: null } })).antenna.reason, /no printed antenna/i);
});

test("a quick review runs no simulation of any kind, and datasheets add nothing to it", () => {
  const o = by(extentOptions({ detection: radio, depth: "quick", keys }));
  for (const id of ["antenna", "power", "thermal", "signal", "datasheets"]) { assert.equal(o[id].available, false, id); assert.match(o[id].reason, /quick/i, id); }
  assert.equal(o.layout.available, true);
  assert.deepEqual(defaultExtent(extentOptions({ detection: radio, depth: "quick" })), ["layout"]);
});

test("datasheets need keys: DigiKey id and secret together, or a Mouser key", () => {
  assert.equal(by(extentOptions({ detection: radio })).datasheets.reason, "Add keys in Settings");
  assert.equal(by(extentOptions({ detection: radio, keys })).datasheets.available, true);
  assert.equal(datasheetKeysSet({ DIGIKEY_CLIENT_ID: { set: true }, DIGIKEY_CLIENT_SECRET: { set: false } }), false);
  assert.equal(datasheetKeysSet({ MOUSER_API_KEY: { set: true } }), true);
  assert.equal(datasheetKeysSet(null), false);
});

test("the simulators can say a check is not possible on this board", () => {
  const o = by(extentOptions({ detection: radio, server: { thermal: { available: false, reason: "No part dissipation is known" } } }));
  assert.equal(o.thermal.available, false); assert.equal(o.thermal.reason, "No part dissipation is known");
  assert.equal(o.power.available, true);
});

test("the default is every simulation the board can run, the Everything preset adds datasheets when keys exist", () => {
  const o = extentOptions({ detection: radio, keys });
  assert.deepEqual(defaultExtent(o), ["layout", "antenna", "power", "thermal", "signal"]);
  assert.deepEqual(everythingExtent(o), ["layout", "antenna", "power", "thermal", "signal", "datasheets"]);
  const n = extentOptions({ detection: noRadio });
  assert.deepEqual(everythingExtent(n), ["layout", "power", "thermal", "signal"]);
  assert.deepEqual(layoutOnly(), ["layout"]);
  assert.equal(sameExtent(everythingExtent(n), ["layout", "power", "thermal", "signal"]), true);
});

test("a selection is cleaned: layout stays, unavailable checks go, the order is fixed", () => {
  const o = extentOptions({ detection: noRadio });
  assert.deepEqual(cleanExtent(["signal", "antenna", "datasheets"], o), ["layout", "signal"]);
  assert.deepEqual(cleanExtent([], o), ["layout"]);
});

test("time cost adds up and is said as a range", () => {
  assert.equal(extentCost(["layout"]), null);
  assert.deepEqual(extentCost(["layout", "power", "thermal"]), [2, 4]);
  assert.equal(costText([2, 6]), "2 to 6 min");
  assert.equal(costText([3, 3]), "about 3 min");
  assert.equal(costLine(null), "No added time");
  assert.equal(costLine([2, 6]), "Adds 2 to 6 min");
});

test("the key list the server sends, mapped by name as getKeyStatus does, enables datasheets", () => {
  const fromServer = { keys: [{ name: "DIGIKEY_CLIENT_ID", set: true, source: "env" }, { name: "DIGIKEY_CLIENT_SECRET", set: true, source: "app" }, { name: "MOUSER_API_KEY", set: false, source: null }] };
  const map = Object.fromEntries(fromServer.keys.map((k) => [k.name, { set: !!k.set, source: k.source ?? undefined }]));
  assert.equal(datasheetKeysSet(map), true);
});
