"use client";

import * as Dialog from "@radix-ui/react-dialog";
import * as Tooltip from "@radix-ui/react-tooltip";
import { CircleCheck, RefreshCw, X } from "lucide-react";
import { useEffect, useRef, useState, type ButtonHTMLAttributes, type PropsWithChildren, type ReactNode } from "react";
import { usePreferences } from "../lib/preferences";
import { cn } from "../lib/utils";
import refreshStyles from "./refresh-button.module.css";

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
type ButtonSize = "sm" | "md";

export function Button({
  className,
  variant = "secondary",
  size = "md",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant;
  size?: ButtonSize;
}) {
  return <button className={cn("button", `button-${variant}`, `button-${size}`, className)} {...props} />;
}

export function IconButton({
  label,
  className,
  children,
  tooltip = true,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode; tooltip?: boolean }) {
  const { t } = usePreferences();
  const button = (
    <button className={cn("icon-button", className)} aria-label={t(label)} type="button" {...props}>
      {children}
    </button>
  );
  if (!tooltip) return button;
  return (
    <Tooltip.Provider delayDuration={250}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>{button}</Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content className="tooltip" sideOffset={8}>
            {t(label)}
            <Tooltip.Arrow className="tooltip-arrow" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}

type RefreshState = "idle" | "refreshing" | "succeeded";

function refreshResultError(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => refreshResultError(item)).find(Boolean);
  }
  if (value && typeof value === "object" && "error" in value) {
    return (value as { error?: unknown }).error;
  }
  return undefined;
}

export function RefreshButton({
  label = "Refresh",
  onRefresh,
  successDurationMs = 2_500,
  disabled = false,
  ...props
}: Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children" | "onClick"> & {
  label?: string;
  onRefresh: () => Promise<unknown> | unknown;
  successDurationMs?: number;
}) {
  const [state, setState] = useState<RefreshState>("idle");
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshLock = useRef(false);

  useEffect(() => () => {
    if (resetTimer.current !== null) clearTimeout(resetTimer.current);
  }, []);

  async function refresh() {
    if (disabled || refreshLock.current || state !== "idle") return;
    refreshLock.current = true;
    setState("refreshing");
    try {
      const result = await onRefresh();
      const error = refreshResultError(result);
      if (error) throw error;
      setState("succeeded");
      resetTimer.current = setTimeout(() => {
        refreshLock.current = false;
        setState("idle");
      }, successDurationMs);
    } catch {
      refreshLock.current = false;
      setState("idle");
    }
  }

  const statusLabel = state === "refreshing" ? "Working..." : state === "succeeded" ? "succeeded" : label;
  return (
    <IconButton
      {...props}
      label={statusLabel}
      disabled={disabled || state !== "idle"}
      data-refresh-state={state}
      onClick={() => void refresh()}
    >
      {state === "succeeded" ? <CircleCheck size={16} /> : <RefreshCw className={state === "refreshing" ? refreshStyles.spinning : undefined} size={16} />}
    </IconButton>
  );
}

export function StatusBadge({ status, className }: { status: string; className?: string }) {
  const { t } = usePreferences();
  const normalized = status.toLowerCase().replaceAll("_", "-");
  const tone = ["online", "in-sync", "healthy", "succeeded", "allowed", "enabled", "valid", "active", "current", "published", "ready"].includes(
    normalized,
  )
    ? "success"
    : ["offline", "failed", "rollback-failed", "unhealthy", "dead-letter", "denied", "shutdown"].includes(
          normalized,
        )
      ? "danger"
      : ["critical"].includes(normalized)
        ? "danger"
        : ["applying", "health-check", "queued", "running", "degraded", "drift", "pending", "attention", "high", "medium", "warning", "expired", "rolled-back"].includes(
            normalized,
          )
          ? "warning"
          : ["resolved"].includes(normalized)
            ? "success"
            : "neutral";
  return <span className={cn("status-badge", `status-${tone}`, className)}>{t(status.replaceAll("_", " "))}</span>;
}

export function Panel({ children, className }: PropsWithChildren<{ className?: string }>) {
  return <section className={cn("panel", className)}>{children}</section>;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  onConfirm,
  busy = false,
  danger = false,
  children,
}: PropsWithChildren<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  confirmLabel: string;
  onConfirm: () => void;
  busy?: boolean;
  danger?: boolean;
}>) {
  const { t } = usePreferences();
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      {children}
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content">
          <div className="dialog-heading">
            <div>
              <Dialog.Title>{t(title)}</Dialog.Title>
              <Dialog.Description>{t(description)}</Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <IconButton label="Close dialog" tooltip={false}>
                <X size={17} />
              </IconButton>
            </Dialog.Close>
          </div>
          <div className="dialog-actions">
            <Dialog.Close asChild>
              <Button disabled={busy}>{t("Cancel")}</Button>
            </Dialog.Close>
            <Button variant={danger ? "danger" : "primary"} disabled={busy} onClick={onConfirm}>
              {busy ? t("Working...") : t(confirmLabel)}
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function DetailDialog({
  open,
  onOpenChange,
  title,
  description,
  contentClassName,
  children,
}: PropsWithChildren<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  contentClassName?: string;
}>) {
  const { t } = usePreferences();
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className={cn("dialog-content detail-dialog-content", contentClassName)}>
          <div className="dialog-heading">
            <div>
              <Dialog.Title>{t(title)}</Dialog.Title>
              {description ? <Dialog.Description>{t(description)}</Dialog.Description> : null}
            </div>
            <Dialog.Close asChild>
              <IconButton label="Close details" tooltip={false}>
                <X size={17} />
              </IconButton>
            </Dialog.Close>
          </div>
          <div className="dialog-body">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
