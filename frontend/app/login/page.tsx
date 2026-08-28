"use client";

import { FormEvent, useState } from "react";
import { ArrowLeft, LockKeyhole } from "lucide-react";
import { Button } from "../../components/ui";

const apiBase = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const response = await fetch(`${apiBase}/api/v1/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
      if (!response.ok) throw new Error("invalid credentials");
      const result = await response.json() as { access_token: string };
      window.localStorage.setItem("grouproxy.management_token", result.access_token);
      window.location.assign("/");
    } catch { setError("Unable to sign in. Check the backend URL and local credentials."); } finally { setBusy(false); }
  }

  return <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24, background: "var(--teal-deep)" }}><div style={{ width: "min(100%, 440px)", borderRadius: 14, padding: 30, background: "#fff" }}><a href="/" style={{ display: "inline-flex", alignItems: "center", gap: 8, color: "var(--muted)", fontSize: 13 }}><ArrowLeft size={15} /> Back to overview</a><div style={{ marginTop: 42, display: "flex", alignItems: "center", gap: 12 }}><span className="feature-icon"><LockKeyhole size={17} /></span><div><p className="eyebrow" style={{ color: "var(--green-dark)", margin: 0 }}>Grouproxy</p><h1 style={{ margin: "5px 0 0", fontSize: 30, fontWeight: 500 }}>Sign in</h1></div></div><p style={{ color: "var(--muted)", fontSize: 14, margin: "18px 0 26px" }}>Use the local administrator account for this test environment.</p><form onSubmit={submit} style={{ display: "grid", gap: 16 }}><div className="field"><label htmlFor="username">Username</label><input id="username" value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" /></div><div className="field"><label htmlFor="password">Password</label><input id="password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" /></div>{error && <div className="error-note">{error}</div>}<Button className="button-primary" type="submit" disabled={busy}>{busy ? "Signing in..." : "Sign in"}</Button></form></div></main>;
}
