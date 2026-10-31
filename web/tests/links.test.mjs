import { test } from "node:test";
import assert from "node:assert/strict";
import { DEFAULT_REPO_URL, githubUrl, discordUrl, webUrl } from "../lib/links.mjs";

test("github chip uses the reported repository link, else the project repository", () => {
  assert.equal(githubUrl("https://github.com/acme/board"), "https://github.com/acme/board");
  assert.equal(githubUrl(null), DEFAULT_REPO_URL);
  assert.equal(githubUrl(""), DEFAULT_REPO_URL);
  assert.equal(githubUrl("javascript:alert(1)"), DEFAULT_REPO_URL);
});

test("discord chip is hidden until a valid web address is set", () => {
  assert.equal(discordUrl(undefined), null);
  assert.equal(discordUrl(""), null);
  assert.equal(discordUrl("  "), null);
  assert.equal(discordUrl("not a url"), null);
  assert.equal(discordUrl("javascript:alert(1)"), null);
  assert.equal(discordUrl("https://discord.gg/abc123"), "https://discord.gg/abc123");
  assert.equal(webUrl(" https://discord.gg/abc123 "), "https://discord.gg/abc123");
});
