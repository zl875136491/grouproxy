"use client";

import { AlertTriangle, RefreshCw, SearchX } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "./ui";

export function LoadingState({ rows = 4 }: { rows?: number }) {
  return (
    <div className="loading-state" aria-label="Loading">
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
  return (
    <div className="empty-state">
      <SearchX size={20} aria-hidden="true" />
      <strong>{title}</strong>
      {detail ? <span>{detail}</span> : null}
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="error-state" role="alert">
      <AlertTriangle size={19} aria-hidden="true" />
      <div>
        <strong>Unable to load this workspace</strong>
        <span>{error}</span>
      </div>
      {onRetry ? <Button size="sm" onClick={onRetry}><RefreshCw size={14} /> Retry</Button> : null}
    </div>
  );
}
