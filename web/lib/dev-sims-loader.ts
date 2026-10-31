// The only door to the dev fixture runs (lib/dev-sims/). The condition is a build-time constant, so a production build leaves the
// fixture code out of every chunk. It is on under `next dev` and when NEXT_PUBLIC_TRUSTPCB_FIXTURES=1.
export const loadDevSims: () => Promise<typeof import("./dev-sims/runs.mjs")> =
  process.env.NODE_ENV === "development" || process.env.NEXT_PUBLIC_TRUSTPCB_FIXTURES === "1"
    ? () => import("./dev-sims/runs.mjs")
    : () => Promise.reject(new Error("Dev fixtures are off."));
export const DEV_SIMS_ON = process.env.NODE_ENV === "development" || process.env.NEXT_PUBLIC_TRUSTPCB_FIXTURES === "1";
