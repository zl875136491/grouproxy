"use client";

import { AlertTriangle, RefreshCw, SearchX } from "lucide-react";
import type { ReactNode } from "react";
import { usePreferences } from "../lib/preferences";
import { Button } from "./ui";

export function LoadingState({ rows = 4 }: { rows?: number }) {
  const { t } = usePreferences();
  return (
    <div className="loading-state" aria-label={t("Loading")}>
      {Array.from({ length: rows }, (_, index) => <span className="loading-line" key={index} />)}
    </div>
  );
}

export function EmptyState({
  title,
  detail,
  action,
}: {
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  const { t } = usePreferences();
  return (
    <div className="empty-state">
      <SearchX size={20} aria-hidden="true" />
      <strong>{t(title)}</strong>
      {detail ? <span>{t(detail)}</span> : null}
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: string; onRetry?: () => void }) {
  const { t } = usePreferences();
  return (
    <div className="error-state" role="alert">
      <AlertTriangle size={19} aria-hidden="true" />
      <div>
        <strong>{t("Unable to load this workspace")}</strong>
        <span>{t(error)}</span>
      </div>
      {onRetry ? <Button size="sm" onClick={onRetry}><RefreshCw size={14} /> {t("Retry")}</Button> : null}
    </div>
  );
}
