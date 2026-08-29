"use client";

import { FormEvent, useState } from "react";
import { ArrowLeft, LockKeyhole } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { saveManagementSession } from "../../lib/api";
import { Button } from "../../components/ui";

const apiBase = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`${apiBase}/api/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) throw new Error("invalid_credentials");
      const result = (await response.json()) as { access_token: string };
      saveManagementSession(result.access_token);
      router.replace("/");
    } catch {
      setError("Sign-in failed. Check the administrator account and control-plane URL.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-panel">
        <Link className="login-back" href="/"><ArrowLeft size={15} /> Back to console</Link>
        <div className="login-heading"><span className="login-icon"><LockKeyhole size={18} /></span><div><span className="page-eyebrow">GROUPROXY</span><h1>Control plane sign-in</h1></div></div>
        <form className="login-form" onSubmit={submit}>
          <label><span>Username</span><input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required /></label>
          <label><span>Password</span><input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" required /></label>
          {error ? <div className="inline-error" role="alert">{error}</div> : null}
          <Button variant="primary" type="submit" disabled={busy}>{busy ? "Signing in..." : "Sign in"}</Button>
        </form>
      </section>
    </main>
  );
}
