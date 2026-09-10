"use client";

import type { ReactNode } from "react";
import { usePreferences } from "../lib/preferences";

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
  icon,
  className,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
  icon?: ReactNode;
  className?: string;
}) {
  const { t } = usePreferences();
  return (
    <header className={`page-header${className ? ` ${className}` : ""}`}>
      <div className="page-header-content">
        {icon ? <span className="page-header-icon" aria-hidden="true">{icon}</span> : null}
        <div className="page-header-copy">
          {eyebrow ? <span className="page-eyebrow">{t(eyebrow)}</span> : null}
          <h1>{t(title)}</h1>
          {description ? <p>{t(description)}</p> : null}
        </div>
      </div>
      {actions ? <div className="page-actions">{actions}</div> : null}
    </header>
  );
}
