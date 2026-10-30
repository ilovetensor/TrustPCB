// Dev tool: screenshot routes with Playwright (headless Chromium, WebGL via ANGLE).
//   node scripts/shot.mjs <outDir> <name>=<path>[@WxH] ...
import { chromium } from "playwright-core";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const [, , outDir, ...specs] = process.argv;
fs.mkdirSync(outDir, { recursive: true });
const exe = path.join(os.homedir(), "Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell");
const browser = await chromium.launch({
  executablePath: exe,
  args: ["--use-gl=angle", "--use-angle=metal", "--enable-webgl", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"],
});
for (const spec of specs) {
  const eq = spec.indexOf("="); const name = spec.slice(0, eq); const rest = spec.slice(eq + 1);
  const [url, size = "1440x900"] = rest.split("@");
  const [w, h] = size.split("x").map(Number);
  const wait = Number(process.env.WAIT ?? 3500);
  const page = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: 1 });
  const logs = [];
  page.on("console", (m) => { if (["error", "warning"].includes(m.type())) logs.push(`${m.type()}: ${m.text().slice(0, 300)}`); });
  page.on("pageerror", (e) => logs.push("pageerror: " + String(e).slice(0, 400)));
  await page.goto(`http://127.0.0.1:3118${url}`, { waitUntil: "load", timeout: 90000 });
  await page.waitForTimeout(wait);
  if (process.env.CLICK) { for (const sel of process.env.CLICK.split("||")) { await page.click(sel).catch(() => logs.push("click fail " + sel)); await page.waitForTimeout(1800); } }
  if (process.env.SCROLL) { await page.evaluate((y) => window.scrollTo(0, y), Number(process.env.SCROLL)); await page.waitForTimeout(1500); }
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  const file = path.join(outDir, `${name}-${w}x${h}.png`);
  await page.screenshot({ path: file, fullPage: process.env.FULL === "1" });
  console.log(file, "hscroll", overflow);
  for (const l of logs.slice(0, 8)) console.log("  ", l);
  await page.close();
}
await browser.close();
