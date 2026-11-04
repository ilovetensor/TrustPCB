// The outside links on the landing page. A link is used only when it is a plain http(s) address, so a mistyped setting never becomes a dead or unsafe link.

export const DEFAULT_REPO_URL = "https://github.com/ilovetensor/TrustPCB";

/** The address when it is a valid http(s) URL, else null. */
export function webUrl(value) {
  const s = typeof value === "string" ? value.trim() : "";
  if (!s) return null;
  try {
    const u = new URL(s);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

/** The GitHub chip: the repository link the backend reports (TRUSTPCB_REPO_URL), else the project's own repository. */
export function githubUrl(repoUrl) {
  return webUrl(repoUrl) ?? DEFAULT_REPO_URL;
}

/** The Discord chip: the invite from NEXT_PUBLIC_DISCORD_URL, or null so the chip stays hidden. */
export function discordUrl(value) {
  return webUrl(value);
}
