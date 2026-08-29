"use client";

import { LockKeyhole } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { hasManagementSession } from "../lib/api";
import { Button } from "./ui";

export function useManagementSession(): boolean | null {
  const [session, setSession] = useState<boolean | null>(null);

  useEffect(() => {
    setSession(hasManagementSession());
  }, []);

  return session;
}

export function SessionGate() {
  return (
    <div className="session-gate">
      <LockKeyhole size={22} aria-hidden="true" />
      <div>
        <h1>Sign in required</h1>
        <p>Use a control-plane administrator account to load operational data.</p>
      </div>
      <Link href="/login"><Button variant="primary">Sign in</Button></Link>
    </div>
  );
}
