import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "../components/app-shell";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Grouproxy 控制平面",
  description: "区域代理运维控制台",
};

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
