// The only door to the dev fixtures (lib/fixture-api.ts and lib/fixtures/). The flag is read here, in this file, so a
// normal build replaces it with a constant and leaves the fixture code out of the production bundles. Switch it on with
// NEXT_PUBLIC_TRUSTPCB_FIXTURES=1 when building or running `next dev`.
export const loadFixtures: () => Promise<typeof import("./fixture-api")> =
  process.env.NEXT_PUBLIC_TRUSTPCB_FIXTURES === "1"
    ? () => import("./fixture-api")
    : () => Promise.reject(new Error("Fixture mode is off."));
