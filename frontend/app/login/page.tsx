"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { ArrowLeft, KeyRound, LockKeyhole, MessageSquareText, UserPlus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  changeAccountPassword,
  loginWithGQuan,
  loginWithPassword,
  registerAccount,
  requestAuthVerificationCode,
  saveManagementSession,
  type VerificationChallenge,
  type VerificationPurpose,
} from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { Button, DetailDialog } from "../../components/ui";

type LoginMode = "password" | "gquan";
type AccountAction = "register" | "password-change";
type AuthBusy = "send" | "submit" | null;

const loginModes: Record<LoginMode, { label: string; purpose?: VerificationPurpose }> = {
  password: { label: "Password" },
  gquan: { label: "GQuan code", purpose: "gquan_login" },
};

const accountActions: Record<
  AccountAction,
  { label: string; title: string; description: string; purpose: VerificationPurpose }
> = {
  register: {
    label: "Create account",
    title: "Create account",
    description: "Verify your IT code before setting a password.",
    purpose: "register",
  },
  "password-change": {
    label: "Reset password",
    title: "Reset password",
    description: "Verify your IT code before choosing a new password.",
    purpose: "password_change",
  },
};

function authErrorMessage(error: unknown, t: (key: string) => string) {
  const code = error instanceof Error ? error.message : "request_failed";
  const messages: Record<string, string> = {
    account_not_registered: "No active account is registered for this IT code.",
    gquan_delivery_unavailable: "GQuan verification delivery is unavailable.",
    gquan_delivery_rejected: "GQuan did not accept the verification request.",
    gquan_quota_exceeded: "GQuan verification quota is currently exhausted.",
    invalid_credentials: "The IT code or password is not valid.",
    invalid_itcode: "Enter a valid IT code.",
    invalid_password: "Passwords must contain at least 12 characters.",
    itcode_already_registered: "This IT code already has an account.",
    password_confirmation_mismatch: "The password confirmation does not match.",
    verification_code_attempts_exceeded: "This code can no longer be used. Request another one.",
    verification_code_expired: "This verification code has expired. Request another one.",
    verification_code_invalid: "The verification code is not valid.",
    verification_code_rate_limited: "A code was sent recently. Wait before requesting another.",
    network_error: "Unable to reach the control plane. Check the local service and retry.",
    validation_error: "The request contains invalid fields.",
  };
  return t(messages[code] || "The request could not be completed.");
}

function VerificationCodeField({
  code,
  onChange,
  onSend,
  canSend,
  busy,
  challenge,
}: {
  code: string;
  onChange: (value: string) => void;
  onSend: () => void;
  canSend: boolean;
  busy: AuthBusy;
  challenge: VerificationChallenge | null;
}) {
  const { t } = usePreferences();
  return (
    <div className="verification-row">
      <label>
        <span>{t("Verification code")}</span>
        <input
          value={code}
          onChange={(event) => onChange(event.target.value.replace(/\D/g, "").slice(0, 6))}
          inputMode="numeric"
          autoComplete="one-time-code"
          required
        />
      </label>
      <Button
        className="verification-send"
        type="button"
        disabled={!canSend || busy !== null}
        onClick={onSend}
      >
        <MessageSquareText size={16} />
        {busy === "send" ? t("Sending...") : challenge ? t("Send again") : t("Send code")}
      </Button>
    </div>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const { t } = usePreferences();
  const [loginMode, setLoginMode] = useState<LoginMode>("password");
  const [loginItcode, setLoginItcode] = useState("");
  const [loginPassword, setLoginPassword] = useState("");
  const [loginVerificationCode, setLoginVerificationCode] = useState("");
  const [loginChallenge, setLoginChallenge] = useState<VerificationChallenge | null>(null);
  const [loginNotice, setLoginNotice] = useState("");
  const [loginError, setLoginError] = useState("");
  const [loginBusy, setLoginBusy] = useState<AuthBusy>(null);

  const [accountAction, setAccountAction] = useState<AccountAction | null>(null);
  const [accountItcode, setAccountItcode] = useState("");
  const [accountPassword, setAccountPassword] = useState("");
  const [accountConfirmation, setAccountConfirmation] = useState("");
  const [accountVerificationCode, setAccountVerificationCode] = useState("");
  const [accountChallenge, setAccountChallenge] = useState<VerificationChallenge | null>(null);
  const [accountNotice, setAccountNotice] = useState("");
  const [accountError, setAccountError] = useState("");
  const [accountBusy, setAccountBusy] = useState<AuthBusy>(null);
  const accountItcodeRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!accountAction) return;
    const frame = window.requestAnimationFrame(() => accountItcodeRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [accountAction]);

  function selectLoginMode(nextMode: LoginMode) {
    setLoginMode(nextMode);
    setLoginPassword("");
    setLoginVerificationCode("");
    setLoginChallenge(null);
    setLoginNotice("");
    setLoginError("");
  }

  function updateLoginItcode(value: string) {
    setLoginItcode(value);
    setLoginChallenge(null);
    setLoginVerificationCode("");
  }

  function resetAccountForm() {
    setAccountItcode("");
    setAccountPassword("");
    setAccountConfirmation("");
    setAccountVerificationCode("");
    setAccountChallenge(null);
    setAccountNotice("");
    setAccountError("");
  }

  function openAccountAction(action: AccountAction) {
    resetAccountForm();
    setAccountItcode(loginItcode);
    setAccountAction(action);
  }

  function closeAccountAction() {
    if (accountBusy !== null) return;
    setAccountAction(null);
    resetAccountForm();
  }

  function updateAccountItcode(value: string) {
    setAccountItcode(value);
    setAccountChallenge(null);
    setAccountVerificationCode("");
  }

  async function sendLoginCode() {
    const purpose = loginModes[loginMode].purpose;
    if (!purpose || !loginItcode.trim()) return;
    setLoginBusy("send");
    setLoginError("");
    setLoginNotice("");
    try {
      const result = await requestAuthVerificationCode(loginItcode.trim(), purpose);
      setLoginChallenge(result);
      setLoginNotice(t("Verification code sent through GQuan."));
    } catch (requestError) {
      setLoginError(authErrorMessage(requestError, t));
    } finally {
      setLoginBusy(null);
    }
  }

  async function sendAccountCode() {
    if (!accountAction || !accountItcode.trim()) return;
    setAccountBusy("send");
    setAccountError("");
    setAccountNotice("");
    try {
      const result = await requestAuthVerificationCode(
        accountItcode.trim(),
        accountActions[accountAction].purpose,
      );
      setAccountChallenge(result);
      setAccountNotice(t("Verification code sent through GQuan."));
    } catch (requestError) {
      setAccountError(authErrorMessage(requestError, t));
    } finally {
      setAccountBusy(null);
    }
  }

  async function submitLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoginBusy("submit");
    setLoginError("");
    setLoginNotice("");
    try {
      if (loginMode === "password") {
        const result = await loginWithPassword(loginItcode.trim(), loginPassword);
        saveManagementSession(result.access_token, result.role);
        router.replace(result.role === "admin" ? "/" : "/access");
        return;
      }
      if (!loginChallenge) throw new Error("verification_code_invalid");
      const result = await loginWithGQuan({
        itcode: loginItcode.trim(),
        challenge_id: loginChallenge.challenge_id,
        verification_code: loginVerificationCode,
      });
      saveManagementSession(result.access_token, result.role);
      router.replace(result.role === "admin" ? "/" : "/access");
    } catch (submitError) {
      setLoginError(authErrorMessage(submitError, t));
    } finally {
      setLoginBusy(null);
    }
  }

  async function submitAccountAction(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!accountAction) return;
    setAccountBusy("submit");
    setAccountError("");
    setAccountNotice("");
    try {
      if (!accountChallenge) throw new Error("verification_code_invalid");
      if (accountPassword !== accountConfirmation) {
        throw new Error("password_confirmation_mismatch");
      }
      const payload = {
        itcode: accountItcode.trim(),
        password: accountPassword,
        challenge_id: accountChallenge.challenge_id,
        verification_code: accountVerificationCode,
      };
      if (accountAction === "register") {
        await registerAccount(payload);
        setLoginNotice(t("Account registered. Sign in with the password you set."));
      } else {
        await changeAccountPassword(payload);
        setLoginNotice(t("Password reset. Sign in with the new password."));
      }
      setLoginItcode(accountItcode.trim());
      setLoginPassword("");
      setLoginMode("password");
      setAccountBusy(null);
      setAccountAction(null);
      resetAccountForm();
    } catch (submitError) {
      setAccountError(authErrorMessage(submitError, t));
      setAccountBusy(null);
    }
  }

  const gquanLogin = loginMode === "gquan";
  const activeAccountAction = accountAction ? accountActions[accountAction] : null;

  return (
    <main className="login-shell">
      <section className="login-panel">
        <Link className="login-back" href="/"><ArrowLeft size={15} /> {t("Back to console")}</Link>
        <div className="login-heading"><span className="login-icon"><LockKeyhole size={18} /></span><div><span className="page-eyebrow">GROUPROXY</span><h1>{t("Control plane access")}</h1></div></div>
        <div className="auth-mode-tabs" role="tablist" aria-label={t("Authentication method")}>
          {(Object.keys(loginModes) as LoginMode[]).map((item) => (
            <button
              aria-selected={loginMode === item}
              className="auth-mode-tab"
              key={item}
              onClick={() => selectLoginMode(item)}
              role="tab"
              type="button"
            >
              {t(loginModes[item].label)}
            </button>
          ))}
        </div>
        <form className="login-form" onSubmit={submitLogin}>
          <label><span>{t("IT code")}</span><input value={loginItcode} onChange={(event) => updateLoginItcode(event.target.value)} autoComplete="username" required /></label>
          {!gquanLogin ? <label><span>{t("Password")}</span><input value={loginPassword} onChange={(event) => setLoginPassword(event.target.value)} type="password" autoComplete="current-password" minLength={12} required /></label> : null}
          {gquanLogin ? <VerificationCodeField code={loginVerificationCode} onChange={setLoginVerificationCode} onSend={() => void sendLoginCode()} canSend={Boolean(loginItcode.trim())} busy={loginBusy} challenge={loginChallenge} /> : null}
          {loginNotice ? <div className="login-notice" role="status">{loginNotice}</div> : null}
          {loginError ? <div className="inline-error" role="alert">{loginError}</div> : null}
          <Button className="login-submit" variant="primary" type="submit" disabled={loginBusy !== null || (gquanLogin && (!loginChallenge || loginVerificationCode.length !== 6))}>
            {loginMode === "password" ? <KeyRound size={16} /> : <LockKeyhole size={16} />}
            {loginBusy === "submit" ? t("Working...") : gquanLogin ? t("Sign in with code") : t("Sign in")}
          </Button>
        </form>
        <div className="login-account-actions" aria-label={t("Account actions")}>
          <Button variant="ghost" size="sm" type="button" disabled={loginBusy !== null} onClick={() => openAccountAction("register")}><UserPlus size={15} /> {t("Create account")}</Button>
          <span aria-hidden="true" />
          <Button variant="ghost" size="sm" type="button" disabled={loginBusy !== null} onClick={() => openAccountAction("password-change")}><KeyRound size={15} /> {t("Reset password")}</Button>
        </div>
      </section>

      {activeAccountAction ? (
        <DetailDialog
          open={Boolean(accountAction)}
          onOpenChange={(open) => { if (!open) closeAccountAction(); }}
          title={activeAccountAction.title}
          description={activeAccountAction.description}
          contentClassName="auth-dialog-content"
        >
          <form className="auth-modal-form" onSubmit={submitAccountAction}>
            <label><span>{t("IT code")}</span><input ref={accountItcodeRef} value={accountItcode} onChange={(event) => updateAccountItcode(event.target.value)} autoComplete="username" required /></label>
            <label><span>{t(accountAction === "register" ? "Password" : "New password")}</span><input value={accountPassword} onChange={(event) => setAccountPassword(event.target.value)} type="password" autoComplete="new-password" minLength={12} required /></label>
            <label><span>{t("Confirm password")}</span><input value={accountConfirmation} onChange={(event) => setAccountConfirmation(event.target.value)} type="password" autoComplete="new-password" minLength={12} required /></label>
            <VerificationCodeField code={accountVerificationCode} onChange={setAccountVerificationCode} onSend={() => void sendAccountCode()} canSend={Boolean(accountItcode.trim())} busy={accountBusy} challenge={accountChallenge} />
            {accountNotice ? <div className="login-notice" role="status">{accountNotice}</div> : null}
            {accountError ? <div className="inline-error" role="alert">{accountError}</div> : null}
            <Button className="auth-modal-submit" variant="primary" type="submit" disabled={accountBusy !== null || !accountChallenge || accountVerificationCode.length !== 6}>
              {accountAction === "register" ? <UserPlus size={16} /> : <LockKeyhole size={16} />}
              {accountBusy === "submit" ? t("Working...") : t(activeAccountAction.label)}
            </Button>
          </form>
        </DetailDialog>
      ) : null}
    </main>
  );
}
