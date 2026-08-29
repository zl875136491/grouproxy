import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "../components/app-shell";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Grouproxy Control Plane",
  description: "Regional proxy operations console",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
