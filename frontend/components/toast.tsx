"use client";

import * as ToastPrimitive from "@radix-ui/react-toast";
import { CircleCheck, CircleX, Info, X } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type PropsWithChildren, type ReactNode } from "react";
import { usePreferences } from "../lib/preferences";
import { cn } from "../lib/utils";

type ToastVariant = "default" | "success" | "destructive";

export type ToastInput = {
  title: string;
  description?: string;
  variant?: ToastVariant;
  duration?: number;
};

type ToastItem = ToastInput & {
  id: number;
  open: boolean;
};

type ToastContextValue = {
  toast: (input: ToastInput) => void;
};

const ToastContext = createContext<ToastContextValue | null>(null);
const toastListeners = new Set<(input: ToastInput) => void>();
const pendingToasts: ToastInput[] = [];

/**
 * Allows non-component callers, including the React Query mutation cache, to
 * surface transient feedback through the same Shadcn-style toast viewport.
 */
export function notifyToast(input: ToastInput) {
  if (toastListeners.size === 0) {
    pendingToasts.push(input);
    if (pendingToasts.length > 5) pendingToasts.shift();
    return;
  }
  for (const listener of toastListeners) listener(input);
}

export function ToastProvider({ children }: PropsWithChildren) {
  const { t } = usePreferences();
  const [items, setItems] = useState<ToastItem[]>([]);

  const toast = useCallback((input: ToastInput) => {
    const id = Date.now() + Math.floor(Math.random() * 1_000_000);
    setItems((current) => [...current.slice(-4), { ...input, id, open: true }]);
  }, []);

  useEffect(() => {
    toastListeners.add(toast);
    for (const input of pendingToasts.splice(0)) toast(input);
    return () => {
      toastListeners.delete(toast);
    };
  }, [toast]);

  const value = useMemo(() => ({ toast }), [toast]);

  return (
    <ToastContext.Provider value={value}>
      <ToastPrimitive.Provider swipeDirection="right">
        {children}
        {items.map((item) => (
          <ToastPrimitive.Root
            className={cn("toast", `toast-${item.variant || "default"}`)}
            duration={item.duration ?? 5_000}
            key={item.id}
            onOpenChange={(open) => {
              if (!open) setItems((current) => current.filter((entry) => entry.id !== item.id));
            }}
            open={item.open}
          >
            <div className="toast-icon" aria-hidden="true">
              {item.variant === "success" ? <CircleCheck size={18} /> : item.variant === "destructive" ? <CircleX size={18} /> : <Info size={18} />}
            </div>
            <div className="toast-content">
              <ToastPrimitive.Title>{item.title}</ToastPrimitive.Title>
              {item.description ? <ToastPrimitive.Description>{item.description}</ToastPrimitive.Description> : null}
            </div>
            <ToastPrimitive.Close className="toast-close" aria-label={t("Dismiss notification")}>
              <X size={16} />
            </ToastPrimitive.Close>
          </ToastPrimitive.Root>
        ))}
        <ToastPrimitive.Viewport className="toast-viewport" />
      </ToastPrimitive.Provider>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) throw new Error("useToast must be used inside ToastProvider");
  return context;
}

export function toastErrorMessage(error: unknown, fallback = "The operation could not be completed.") {
  return error instanceof Error && error.message ? error.message : fallback;
}

export type ToastAction = {
  label: string;
  onClick: () => void;
  icon?: ReactNode;
};
