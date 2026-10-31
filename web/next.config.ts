import type { NextConfig } from "next";

// The fixture replay is a design aid, switched on only by NEXT_PUBLIC_TRUSTPCB_FIXTURES=1 at build time. Setting the
// value here (also when it is off) makes the build replace it with a constant, so a normal build drops the fixture code.
const config: NextConfig = {
  poweredByHeader: false,
  devIndicators: false,
  env: { NEXT_PUBLIC_TRUSTPCB_FIXTURES: process.env.NEXT_PUBLIC_TRUSTPCB_FIXTURES === "1" ? "1" : "0" },
  async headers() {
    // the bundled sample runs never change under one build: let browsers keep them (not in dev, where the files get swapped)
    if (process.env.NODE_ENV !== "production") return [];
    return [{ source: "/fixtures/:path*", headers: [{ key: "Cache-Control", value: "public, max-age=86400" }] }];
  },
};
export default config;
