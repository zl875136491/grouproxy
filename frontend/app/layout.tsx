import type { Metadata } from "next";
import "@xyflow/react/dist/style.css";
import "./globals.css";
import { AppShell } from "../components/app-shell";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Grouproxy 控制平面",
  description: "区域代理运维控制台",
};

// The dashboard shell contains client-side queries and session state. Do not
// let Next.js cache an old shell that still references a previous chunk after
// a dashboard deployment.
export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
