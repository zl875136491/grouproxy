"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  ArrowLeftRight,
  Ban,
  BookOpen,
  Boxes,
  ClipboardList,
  FileClock,
  Gauge,
  KeyRound,
  LogOut,
  Menu,
  Network,
  Radio,
  RefreshCw,
  ScrollText,
  ServerCog,
  ShieldCheck,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type ComponentType, type PropsWithChildren } from "react";
import { clearManagementSession, logoutManagementSession } from "../lib/api";
import { usePreferences } from "../lib/preferences";
import { cn } from "../lib/utils";
import { PreferencesControls } from "./preferences-controls";
import { IconButton, StatusBadge } from "./ui";

type NavigationItem = {
  href: string;
  label: string;
  icon: ComponentType<{ size?: number; strokeWidth?: number }>;
  matches?: string[];
};

const navigation: Array<{ label: string; items: NavigationItem[] }> = [
  {
    label: "OPERATE",
    items: [
      { href: "/", label: "Overview", icon: Gauge },
      { href: "/nodes", label: "Nodes", icon: ServerCog },
    ],
  },
  {
    label: "POLICY",
    items: [
      { href: "/sites", label: "Sites & CIDRs", icon: Network, matches: ["/sites"] },
      { href: "/exceptions", label: "Exceptions", icon: KeyRound },
      { href: "/cross-site", label: "Cross-site", icon: ArrowLeftRight },
      { href: "/blacklist", label: "Destination deny", icon: Ban },
    ],
  },
  {
    label: "DEPLOY",
    items: [
      { href: "/subscriptions", label: "Subscriptions", icon: Radio },
      { href: "/releases", label: "Releases", icon: Boxes },
      { href: "/tasks", label: "Tasks", icon: ClipboardList },
    ],
  },
  {
    label: "GOVERN",
    items: [
      { href: "/audit", label: "Audit", icon: ScrollText },
      { href: "/access", label: "Access", icon: BookOpen },
    ],
  },
];

function isCurrent(pathname: string, item: NavigationItem) {
  return item.href === "/" ? pathname === "/" : (item.matches || [item.href]).some((path) => pathname.startsWith(path));
}

function pageLabel(pathname: string) {
  for (const group of navigation) {
    const match = group.items.find((item) => isCurrent(pathname, item));
    if (match) return match.label;
  }
  return "Control plane";
}

export function AppShell({ children }: PropsWithChildren) {
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  const { t } = usePreferences();
  const [mobileOpen, setMobileOpen] = useState(false);

  if (pathname === "/login") return <><PreferencesControls className="login-preferences" />{children}</>;

  function closeMobile() {
    setMobileOpen(false);
  }

  async function signOut() {
    try {
      await logoutManagementSession();
    } finally {
      clearManagementSession();
      queryClient.clear();
      router.replace("/login");
    }
  }

  return (
    <div className="app-shell">
      <button
        className={cn("mobile-backdrop", mobileOpen && "mobile-backdrop-visible")}
        aria-label={t("Close navigation")}
        onClick={closeMobile}
      />
      <aside className={cn("sidebar", mobileOpen && "sidebar-open")}>
        <div className="sidebar-brand">
          <Link href="/" onClick={closeMobile} className="brand-link">
            <span className="brand-mark" aria-hidden="true" />
            <span>grouproxy</span>
          </Link>
          <IconButton label={t("Close navigation")} className="mobile-only" onClick={closeMobile}>
            <X size={18} />
          </IconButton>
        </div>
        <nav className="sidebar-nav" aria-label={t("Primary navigation")}>
          {navigation.map((group) => (
            <div className="nav-group" key={group.label}>
              <span className="nav-group-label">{t(group.label)}</span>
              {group.items.map((item) => {
                const active = isCurrent(pathname, item);
                const Icon = item.icon;
                return (
                  <Link
                    aria-current={active ? "page" : undefined}
                    className={cn("nav-item", active && "nav-item-active")}
                    href={item.href}
                    key={item.href}
                    onClick={closeMobile}
                  >
                    <Icon size={17} />
                    <span>{t(item.label)}</span>
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="transport-note">
            <ShieldCheck size={15} />
            <div><span>{t("Employee path")}</span><strong>HTTP CONNECT :80</strong></div>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="topbar-context">
            <IconButton label={t("Open navigation")} className="mobile-menu" onClick={() => setMobileOpen(true)}>
              <Menu size={19} />
            </IconButton>
            <span className="topbar-product">{t("Control plane")}</span>
            <span className="topbar-separator">/</span>
            <strong>{t(pageLabel(pathname))}</strong>
          </div>
          <div className="topbar-actions">
            <StatusBadge status="HTTP only" />
            <PreferencesControls />
            <IconButton label={t("Refresh workspace")} onClick={() => queryClient.invalidateQueries()}>
              <RefreshCw size={16} />
            </IconButton>
            <IconButton label={t("Sign out")} onClick={() => void signOut()}>
              <LogOut size={16} />
            </IconButton>
          </div>
        </header>
        <main className="app-main">{children}</main>
      </div>
    </div>
  );
}
