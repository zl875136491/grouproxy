"use client";

import type { ReactNode } from "react";
import { usePreferences } from "../lib/preferences";

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  const { t } = usePreferences();
  return (
    <header className="page-header">
      <div>
        {eyebrow ? <span className="page-eyebrow">{t(eyebrow)}</span> : null}
        <h1>{t(title)}</h1>
        {description ? <p>{t(description)}</p> : null}
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}
