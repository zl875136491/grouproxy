import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Grouproxy Control Plane",
  description: "Regional proxy access and release operations",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
