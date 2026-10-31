// Dev tool: screenshot the workspace of a finished review as it looked at an earlier event (a real log, cut short).
//   node scripts/shot-replay.mjs <out.png> <reviewId> <eventsJson> <lastSequence> [WxH]
import { chromium } from "playwright-core";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
const [, , out, review, eventsFile, last, size = "1440x900"] = process.argv;
const events = JSON.parse(fs.readFileSync(eventsFile)).filter((e) => e.sequence <= Number(last));
const [w, h] = size.split("x").map(Number);
// CHROME_BIN overrides the Playwright cache location (the default is the macOS headless shell)
const exe = process.env.CHROME_BIN ?? path.join(os.homedir(), "Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell");
const browser = await chromium.launch({ executablePath: exe, args: ["--use-gl=angle", "--use-angle=metal", "--enable-webgl", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"] });
const page = await browser.newPage({ viewport: { width: w, height: h } });
await page.route(`**/api/reviews/${review}/stream*`, (r) => r.fulfill({ status: 200, contentType: "text/event-stream", body: events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join("") }));
await page.goto(`http://127.0.0.1:3118/reviews/${review}`, { waitUntil: "load" });
await page.waitForTimeout(Number(process.env.WAIT ?? 7000));
if (process.env.SCROLLEL) { const [sel, y] = process.env.SCROLLEL.split("::"); await page.evaluate(([q, v]) => { const el = document.querySelector(q); if (el) el.scrollTop = Number(v); }, [sel, y]); await page.waitForTimeout(500); }
await page.screenshot({ path: out });
console.log(out);
await browser.close();
