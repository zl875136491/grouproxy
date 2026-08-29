"use client";

import * as Dialog from "@radix-ui/react-dialog";
import * as Tooltip from "@radix-ui/react-tooltip";
import { X } from "lucide-react";
import type { ButtonHTMLAttributes, PropsWithChildren, ReactNode } from "react";
import { cn } from "../lib/utils";

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
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode }) {
  return (
    <Tooltip.Provider delayDuration={250}>
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <button className={cn("icon-button", className)} aria-label={label} type="button" {...props}>
            {children}
          </button>
        </Tooltip.Trigger>
        <Tooltip.Portal>
          <Tooltip.Content className="tooltip" sideOffset={8}>
            {label}
            <Tooltip.Arrow className="tooltip-arrow" />
          </Tooltip.Content>
        </Tooltip.Portal>
      </Tooltip.Root>
    </Tooltip.Provider>
  );
}

export function StatusBadge({ status, className }: { status: string; className?: string }) {
  const normalized = status.toLowerCase().replaceAll("_", "-");
  const tone = ["online", "in-sync", "healthy", "succeeded", "allowed", "enabled", "valid", "active", "current", "published", "ready"].includes(
    normalized,
  )
    ? "success"
    : ["offline", "failed", "rollback-failed", "unhealthy", "dead-letter", "denied", "shutdown"].includes(
          normalized,
        )
      ? "danger"
      : ["applying", "health-check", "queued", "running", "degraded", "drift", "pending", "attention", "high", "medium", "expired", "rolled-back"].includes(
            normalized,
          )
        ? "warning"
        : "neutral";
  return <span className={cn("status-badge", `status-${tone}`, className)}>{status.replaceAll("_", " ")}</span>;
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
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      {children}
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content">
          <div className="dialog-heading">
            <div>
              <Dialog.Title>{title}</Dialog.Title>
              <Dialog.Description>{description}</Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <IconButton label="Close dialog">
                <X size={17} />
              </IconButton>
            </Dialog.Close>
          </div>
          <div className="dialog-actions">
            <Dialog.Close asChild>
              <Button disabled={busy}>Cancel</Button>
            </Dialog.Close>
            <Button variant={danger ? "danger" : "primary"} disabled={busy} onClick={onConfirm}>
              {busy ? "Working..." : confirmLabel}
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
  children,
}: PropsWithChildren<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
}>) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content detail-dialog-content">
          <div className="dialog-heading">
            <div>
              <Dialog.Title>{title}</Dialog.Title>
              {description ? <Dialog.Description>{description}</Dialog.Description> : null}
            </div>
            <Dialog.Close asChild>
              <IconButton label="Close details">
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
