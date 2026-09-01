"use client";

import { LockKeyhole } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { hasAuthenticatedSession, hasManagementSession } from "../lib/api";
import { usePreferences } from "../lib/preferences";
import { Button } from "./ui";

export function useManagementSession(): boolean | null {
  const [session, setSession] = useState<boolean | null>(null);

  useEffect(() => {
    setSession(hasManagementSession());
  }, []);

  return session;
}

export function useAuthenticatedSession(): boolean | null {
  const [session, setSession] = useState<boolean | null>(null);

  useEffect(() => {
    setSession(hasAuthenticatedSession());
  }, []);

  return session;
}

export function SessionGate() {
  const { t } = usePreferences();
  return (
    <div className="session-gate">
      <LockKeyhole size={22} aria-hidden="true" />
      <div>
        <h1>{t("Sign in required")}</h1>
        <p>{t("Use a control-plane administrator account to load operational data.")}</p>
      </div>
      <Link href="/login"><Button variant="primary">{t("Sign in")}</Button></Link>
    </div>
  );
}
