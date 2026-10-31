import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { sanitizeSvgInner, ALLOWED_ELEMENTS, ALLOWED_ATTRIBUTES } from "../lib/svg-sanitize.mjs";

// DOMParser only exists in a browser, so the parsing cases run in headless Chromium (playwright-core is a dev dependency).
// Set CHROME_BIN to a Chromium or headless shell binary; the default is the Playwright cache on macOS and Linux.
const candidates = [
  process.env.CHROME_BIN,
  path.join(os.homedir(), "Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell"),
].filter(Boolean);
const exe = candidates.find((p) => existsSync(p));
const noBrowser = exe ? false : "no Chromium binary found (set CHROME_BIN)";

const NS = 'xmlns="http://www.w3.org/2000/svg"';
const wrap = (inner, extra = "") => `<svg ${NS} viewBox="0 0 10 10" ${extra}>${inner}</svg>`;

// shapes copied from a real layer set (silk, references, courtyard)
const REAL_SILK = wrap('<g id="layer-F.SilkS" data-role="silk" stroke="#f4f1c4" fill="none" stroke-linecap="round" stroke-linejoin="round"><path data-owner="TP3" d="M14.95 25 L14.912 25.268 Z" stroke-width="0.12"/><polyline data-owner="R5" points="43.977,15.737 43.977,15.263" stroke-width="0.12"/><circle data-id="ref:C1" cx="3" cy="4" r="0.5" stroke-width="0.1"/><line x1="0" y1="0" x2="1" y2="1" stroke-width="0.1"/></g>');
const REAL_REFS = wrap('<g id="layer-F.Refs" data-role="refs"><text x="14.005" y="25" font-family="Inter, Helvetica, Arial, sans-serif" font-size="1" font-weight="600" text-anchor="middle" dominant-baseline="central" fill="#ffffff" stroke="#0e1115" stroke-width="0.22" paint-order="stroke" stroke-linejoin="round">TP3</text></g>');
const REAL_YARD = wrap('<g id="layer-F.CrtYd" data-role="courtyard" stroke="#d36bd3" fill="none" stroke-width="0.06" stroke-dasharray="0.4 0.25"><path data-id="ref:TP3" d="M15.25 25 L15.211 25.311 Z"/></g>');

let browser, page;
before(async () => {
  if (noBrowser) return;
  const { chromium } = await import("playwright-core");
  browser = await chromium.launch({ executablePath: exe });
  page = await browser.newPage();
  await page.setContent("<!doctype html><title>t</title>");
  const src = readFileSync(new URL("../lib/svg-sanitize.mjs", import.meta.url), "utf8");
  await page.addScriptTag({ type: "module", content: `${src}\nwindow.sanitizeSvgInner = sanitizeSvgInner;` });
  await page.waitForFunction(() => typeof window.sanitizeSvgInner === "function");
});
after(async () => { await browser?.close(); });

const run = (svg) => page.evaluate((s) => window.sanitizeSvgInner(s), svg);
/** Render the result the way the app does and report what ended up in the live DOM. */
const live = (markup) => page.evaluate((m) => {
  const host = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  host.innerHTML = m ?? "";
  const els = [...host.querySelectorAll("*")];
  return {
    tags: els.map((e) => e.localName),
    attrs: els.flatMap((e) => [...e.attributes].map((a) => a.name)),
    values: els.flatMap((e) => [...e.attributes].map((a) => a.value)),
  };
}, markup);

test("the module loads and returns null without a DOMParser (server rendering)", () => {
  assert.equal(typeof globalThis.DOMParser, "undefined");
  assert.equal(sanitizeSvgInner(REAL_SILK), null);
  assert.ok(ALLOWED_ELEMENTS.has("path") && !ALLOWED_ELEMENTS.has("script") && !ALLOWED_ELEMENTS.has("foreignObject"));
  assert.ok(ALLOWED_ATTRIBUTES.has("d") && !ALLOWED_ATTRIBUTES.has("href") && !ALLOWED_ATTRIBUTES.has("style"));
});

test("real layer markup passes through unchanged", { skip: noBrowser }, async () => {
  for (const svg of [REAL_SILK, REAL_REFS, REAL_YARD]) {
    const out = await run(svg);
    const innerOfInput = svg.slice(svg.indexOf(">") + 1, svg.lastIndexOf("</svg>"));
    // the only difference allowed is the dropped id attribute and self closing tags
    const norm = (s) => s.replace(/ id="[^"]*"/g, "").replace(/<(\w+)([^>]*?)><\/\1>/g, "<$1$2/>");
    assert.equal(norm(out), norm(innerOfInput));
  }
});

test("text content survives and is escaped", { skip: noBrowser }, async () => {
  const out = await run(wrap('<text x="1" y="2">R&amp;5 &lt;b&gt;</text>'));
  assert.equal(out, '<text x="1" y="2">R&amp;5 &lt;b&gt;</text>');
});

test("script and style elements are removed with their content", { skip: noBrowser }, async () => {
  const out = await run(wrap('<script>alert(1)</script><style>*{fill:red}</style><g><script>x()</script><path d="M0 0L1 1"/></g>'));
  assert.equal(out, '<g><path d="M0 0L1 1"/></g>');
});

test("event handlers are removed, including ones with no space before them", { skip: noBrowser }, async () => {
  const cases = [
    wrap('<g onload="alert(1)"><path d="M0 0" onclick="x()"/></g>'),
    wrap('<g/onload="alert(1)"><path d="M0 0"/></g>'), // not well formed XML, so refused outright
    wrap('<g onLoad="alert(1)" ONCLICK="x()" onmouseover=\'x()\'><circle cx="1" cy="1" r="1" onfocus="x()"/></g>'),
    wrap('<path d="M0 0" onerror = "x()"/>'),
  ];
  for (const svg of cases) {
    const out = await run(svg);
    if (out === null) continue;
    assert.doesNotMatch(out, /on[a-z]+\s*=/i, out);
    const l = await live(out);
    assert.ok(!l.attrs.some((a) => /^on/i.test(a)), l.attrs.join());
  }
});

test("foreignObject and embedded html or svg documents are removed", { skip: noBrowser }, async () => {
  const out = await run(wrap('<foreignObject width="10" height="10"><body xmlns="http://www.w3.org/1999/xhtml"><img src="x" onerror="alert(1)"/></body></foreignObject><g><path d="M0 0"/></g>'));
  assert.equal(out, '<g><path d="M0 0"/></g>');
  const html = await run(wrap('<html:script xmlns:html="http://www.w3.org/1999/xhtml">alert(1)</html:script><html:iframe xmlns:html="http://www.w3.org/1999/xhtml" src="javascript:alert(1)"/>'));
  assert.equal(html, "");
});

test("image, use, a, animate and set elements are removed", { skip: noBrowser }, async () => {
  const x = 'xmlns:xlink="http://www.w3.org/1999/xlink"';
  const svg = `<svg ${NS} ${x}><image href="http://evil.test/p.png" width="1" height="1"/><image xlink:href="https://evil.test/p.png"/><use href="data:image/svg+xml,&lt;svg/&gt;#a"/><use xlink:href="#b"/><a href="javascript:alert(1)"><text>click</text></a><a xlink:href="http://evil.test"><path d="M0 0"/></a><animate attributeName="href" values="javascript:alert(1)"/><set attributeName="onload" to="alert(1)"/><animateTransform attributeName="transform"/><g><path d="M1 1"/></g></svg>`;
  const out = await run(svg);
  assert.equal(out, '<g><path d="M1 1"/></g>');
});

test("href, style and other attributes outside the list are removed from allowed elements", { skip: noBrowser }, async () => {
  const x = 'xmlns:xlink="http://www.w3.org/1999/xlink"';
  const svg = `<svg ${NS} ${x}><path d="M0 0" href="http://evil.test" xlink:href="http://evil.test" style="fill:url(http://evil.test/a)" id="x" filter="url(#f)" mask="url(#m)" src="x" requiredExtensions="x"/></svg>`;
  assert.equal(await run(svg), '<path d="M0 0"/>');
});

test("attribute values that reach outside or script are dropped", { skip: noBrowser }, async () => {
  const svg = wrap('<path d="M0 0" fill="url(http://evil.test/x#a)" stroke="url(#local)"/><path d="M1 1" fill="javascript:alert(1)"/><path d="M2 2" class="a&lt;b" fill="red"/><text font-family="x; background:url(a)">t</text>');
  const out = await run(svg);
  assert.equal(out, '<path d="M0 0"/><path d="M1 1"/><path d="M2 2" fill="red"/><text>t</text>');
});

test("data attributes are kept, and cannot break out of the attribute", { skip: noBrowser }, async () => {
  const out = await run(wrap('<g data-role="silk" data-net="a&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"><path d="M0 0"/></g>'));
  assert.equal(out, '<g data-role="silk" data-net="a&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"><path d="M0 0"/></g>');
  const l = await live(out);
  assert.deepEqual(l.tags, ["g", "path"]);
});

test("entity declarations, doctypes, malformed and non-svg input return null", { skip: noBrowser }, async () => {
  assert.equal(await run('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><svg xmlns="http://www.w3.org/2000/svg"><text>&b;</text></svg>'), null);
  assert.equal(await run('<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://evil.test/x.dtd"><svg xmlns="http://www.w3.org/2000/svg"/>'), null);
  assert.equal(await run('<svg xmlns="http://www.w3.org/2000/svg"><g></svg>'), null);
  assert.equal(await run("<html><body><script>alert(1)</script></body></html>"), null);
  assert.equal(await run('<div xmlns="http://www.w3.org/1999/xhtml"/>'), null);
  assert.equal(await run(""), null);
});

test("mXSS style nesting cannot add elements after the result is parsed as html", { skip: noBrowser }, async () => {
  const svg = wrap('<g><text><![CDATA[</text><script>alert(1)</script>]]></text></g><title><![CDATA[<img src=x onerror=alert(1)>]]></title>');
  const out = await run(svg);
  const l = await live(out);
  assert.ok(!l.tags.includes("script") && !l.tags.includes("img"), l.tags.join());
  assert.match(out, /&lt;script&gt;/);
});

test("rendering the output never creates a script, handler or external reference", { skip: noBrowser }, async () => {
  const nasty = wrap(
    '<g onload="a()"><script>a()</script><image href="http://e.test/a.png"/><use href="#x"/><foreignObject><div onclick="a()"/></foreignObject><a href="javascript:a()"><text x="1" y="1" onclick="a()">ok</text></a><path d="M0 0L1 1" fill="url(http://e.test)" style="x:y" onmouseover="a()"/></g>',
  );
  const l = await live(await run(nasty));
  for (const t of l.tags) assert.ok(ALLOWED_ELEMENTS.has(t), t);
  for (const a of l.attrs) assert.ok(ALLOWED_ATTRIBUTES.has(a) || a.startsWith("data-"), a);
  assert.ok(!l.values.some((v) => /http|javascript|url\(/i.test(v)), l.values.join());
});
