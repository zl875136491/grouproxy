"use client";

import { useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Archive,
  ArrowLeftRight,
  Ban,
  BellRing,
  BookOpen,
  Boxes,
  ClipboardList,
  FileClock,
  Gauge,
  KeyRound,
  LogOut,
  Menu,
  Network,
  PanelLeftClose,
  PanelLeftOpen,
  Radio,
  ScrollText,
  ServerCog,
  UsersRound,
  Waypoints,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ComponentType, type PropsWithChildren } from "react";
import { clearManagementSession, logoutManagementSession, managementSessionRole, type SessionRole } from "../lib/api";
import { usePreferences } from "../lib/preferences";
import { cn } from "../lib/utils";
import { PreferencesControls } from "./preferences-controls";
import { ConfirmDialog, IconButton, RefreshButton } from "./ui";

const sidebarCollapsedStorageKey = "grouproxy.sidebar.collapsed";

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
      { href: "/proxies", label: "Outbound services", icon: Waypoints },
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
    label: "OBSERVE",
    items: [
      { href: "/logs", label: "Logs", icon: ScrollText },
      { href: "/connections", label: "Connections", icon: Activity },
      { href: "/probes", label: "Probes", icon: Radio },
      { href: "/alerts", label: "Alerts", icon: BellRing },
    ],
  },
  {
    label: "GOVERN",
    items: [
      { href: "/employees", label: "Employees", icon: UsersRound },
      { href: "/audit", label: "Audit", icon: ScrollText },
      { href: "/backups", label: "Backups", icon: Archive },
      { href: "/access", label: "Access", icon: BookOpen },
    ],
  },
];

const employeeNavigation: Array<{ label: string; items: NavigationItem[] }> = [
  { label: "GOVERN", items: [{ href: "/access", label: "Access", icon: BookOpen }] },
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
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [signOutConfirmOpen, setSignOutConfirmOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [role, setRole] = useState<SessionRole | null>(null);

  useEffect(() => {
    setRole(managementSessionRole());
  }, [pathname]);

  useEffect(() => {
    setSidebarCollapsed(window.localStorage.getItem(sidebarCollapsedStorageKey) === "true");
  }, []);

  if (pathname === "/login") return <><PreferencesControls className="login-preferences" />{children}</>;

  function closeMobile() {
    setMobileOpen(false);
  }

  function toggleSidebar() {
    setSidebarCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem(sidebarCollapsedStorageKey, String(next));
      return next;
    });
  }

  const activeNavigation = role === "employee" ? employeeNavigation : navigation;

  async function signOut() {
    if (signingOut) return;
    setSigningOut(true);
    try {
      await logoutManagementSession();
    } finally {
      clearManagementSession();
      queryClient.clear();
      setSignOutConfirmOpen(false);
      router.replace("/login");
    }
  }

  return (
    <div className={cn("app-shell", sidebarCollapsed && "app-shell-sidebar-collapsed")}>
      <button
        className={cn("mobile-backdrop", mobileOpen && "mobile-backdrop-visible")}
        aria-label={t("Close navigation")}
        onClick={closeMobile}
      />
      <aside className={cn("sidebar", mobileOpen && "sidebar-open")}>
        <div className="sidebar-brand">
          <Link href="/" onClick={closeMobile} className="brand-link">
            <span className="brand-mark" aria-hidden="true" />
            <span className="brand-wordmark">Grouproxy</span>
          </Link>
          <IconButton label={t("Close navigation")} className="mobile-only" onClick={closeMobile}>
            <X size={18} />
          </IconButton>
        </div>
        <nav className="sidebar-nav" aria-label={t("Primary navigation")}>
          {activeNavigation.map((group) => (
            <div className="nav-group" key={group.label}>
              <span className="nav-group-label">{t(group.label)}</span>
              {group.items.map((item) => {
                const active = isCurrent(pathname, item);
                const Icon = item.icon;
                return (
                  <Link
                    aria-label={t(item.label)}
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
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="topbar-context">
            <IconButton
              label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
              className="sidebar-trigger"
              onClick={toggleSidebar}
            >
              {sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
            </IconButton>
            <IconButton label={t("Open navigation")} className="mobile-menu" onClick={() => setMobileOpen(true)}>
              <Menu size={19} />
            </IconButton>
            <span className="topbar-product">{t("Control plane")}</span>
            <span className="topbar-separator">/</span>
            <strong>{t(pageLabel(pathname))}</strong>
          </div>
          <div className="topbar-actions">
            <PreferencesControls />
            <RefreshButton label="Refresh workspace" onRefresh={() => queryClient.invalidateQueries()} />
            <IconButton label={t("Sign out")} disabled={signingOut} onClick={() => setSignOutConfirmOpen(true)}>
              <LogOut size={16} />
            </IconButton>
          </div>
        </header>
        <main className="app-main">{children}</main>
      </div>
      <ConfirmDialog
        open={signOutConfirmOpen}
        onOpenChange={(open) => {
          if (!signingOut) setSignOutConfirmOpen(open);
        }}
        title="Sign out of the control plane?"
        description="You will need to sign in again to access the control plane."
        confirmLabel="Sign out"
        onConfirm={() => void signOut()}
        busy={signingOut}
        danger
      />
    </div>
  );
}
