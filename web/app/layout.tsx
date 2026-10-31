import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const ui = Inter({ subsets: ["latin"], variable: "--f-ui", display: "swap" });

export const metadata: Metadata = {
  title: "TrustPCB. Others read your datasheets. TrustPCB tests your board.",
  description: "An open-source inspection layer for circuit boards: marked-up boards, real simulations, tested recommendations.",
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
