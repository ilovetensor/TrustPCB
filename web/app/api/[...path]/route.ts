import { allowedRoute, isArtifactRoute, trustedRequest } from "../../../lib/proxy-policy.mjs";
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
async function proxy(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
) {
  if (
    !trustedRequest(
      request.headers.get("host"),
      request.headers.get("origin"),
      request.method,
    )
  )
    return Response.json(
      { detail: "Untrusted local request." },
      { status: 403 },
    );
  const { path: parts } = await context.params;
  const path = parts.join("/");
  if (!allowedRoute(request.method, path))
    return Response.json({ detail: "Unknown API route." }, { status: 404 });
  const token = process.env.TRUSTPCB_TOKEN;
  if (!token)
    return Response.json(
      {
        detail:
          "TRUSTPCB_TOKEN is missing. Start TrustPCB with its local launcher.",
      },
      { status: 503 },
    );
  const backend = (
    process.env.TRUSTPCB_BACKEND_URL || "http://127.0.0.1:8118"
  ).replace(/\/$/, "");
  const incoming = new URL(request.url);
  const target = new URL(`${backend}/api/${path}`);
  for (const key of ["after", "path", "from", "to"]) {
    const value = incoming.searchParams.get(key);
    if (value !== null) target.searchParams.set(key, value);
  }
  const headers = new Headers({
    "X-TrustPCB-Token": token,
    Origin: "http://127.0.0.1:3118",
  });
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body:
        request.method === "GET" || request.method === "HEAD"
          ? undefined
          : request.body,
      signal: request.signal,
      cache: "no-store",
      redirect: "error",
      ...({ duplex: "half" } as Record<string, string>),
    });
    // Version artifacts (board.json, scene SVGs, the GLB) are immutable per version, so the browser
    // may keep them for the short time the backend allows. Everything else is never cached.
    const isArtifact = isArtifactRoute(request.method, path);
    const outgoing = new Headers({
      "Cache-Control": isArtifact
        ? (upstream.headers.get("cache-control") ?? "private, max-age=60")
        : "no-store",
      "X-Content-Type-Options": "nosniff",
    });
    // Keep the backend's sandbox policy on report HTML and artifacts, and the exact media type
    // (model/gltf-binary, image/svg+xml). The body is streamed, never buffered, so a 7 MB GLB
    // reaches the viewer as it is read.
    for (const name of [
      "content-type",
      "content-length",
      "content-disposition",
      "content-security-policy",
    ]) {
      const value = upstream.headers.get(name);
      if (value) outgoing.set(name, value);
    }
    if (upstream.headers.get("content-type")?.includes("text/event-stream"))
      outgoing.set("X-Accel-Buffering", "no");
    return new Response(upstream.body, {
      status: upstream.status,
      headers: outgoing,
    });
  } catch {
    if (request.signal.aborted) return new Response(null, { status: 499 });
    return Response.json(
      {
        detail:
          "The local TrustPCB service is unavailable. Check the launcher and retry.",
      },
      { status: 502 },
    );
  }
}
export { proxy as GET, proxy as POST, proxy as PATCH, proxy as PUT, proxy as DELETE };
