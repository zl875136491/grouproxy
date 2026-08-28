import type { ButtonHTMLAttributes, HTMLAttributes, PropsWithChildren } from "react";

export function Button({ className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={`button ${className}`} {...props} />;
}

export function Card({ className = "", ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={`module ${className}`} {...props} />;
}

export function Badge({ children, tone = "default" }: PropsWithChildren<{ tone?: "default" | "warn" }>) {
  return <span className={`status-pill${tone === "warn" ? " warn" : ""}`}>{children}</span>;
}
