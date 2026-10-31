import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const ui = Inter({ subsets: ["latin"], variable: "--f-ui", display: "swap" });

export const metadata: Metadata = {
  title: "TrustPCB. Catch board problems before you pay to build them.",
  description: "Upload a KiCad board. Every issue is marked on the design, measured and explained, and antennas are tested in simulation. Your design is never changed.",
};
export const viewport: Viewport = { themeColor: "#000000", width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={ui.variable}>
      <body>
        <a className="sr skip" href="#main">Skip to content</a>
        {children}
      </body>
    </html>
  );
}
