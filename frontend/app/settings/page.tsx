"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, Settings2 } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { getSystemSettings, updateSystemSettings } from "../../lib/api";
import { usePreferences } from "../../lib/preferences";
import { ErrorState, LoadingState } from "../../components/data-state";
import { ManagementAdminGate, SessionGate, useAuthenticatedSession, useManagementSession } from "../../components/session-gate";
import { PageHeader } from "../../components/page-header";
import { Button, Panel } from "../../components/ui";
import { notifyToast, toastErrorMessage } from "../../components/toast";

const DEFAULT_GREEN_MAX_MS = 100;
const DEFAULT_YELLOW_MAX_MS = 200;
const MAX_LATENCY_BOUNDARY_MS = 300_000;

function parseBoundary(value: string): number | null {
  if (!/^\d+$/.test(value.trim())) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed <= MAX_LATENCY_BOUNDARY_MS ? parsed : null;
}

export default function SettingsPage() {
  const { t, formatNumber } = usePreferences();
  const authenticated = useAuthenticatedSession();
  const management = useManagementSession();
  const queryClient = useQueryClient();
  const [greenMax, setGreenMax] = useState(String(DEFAULT_GREEN_MAX_MS));
  const [yellowMax, setYellowMax] = useState(String(DEFAULT_YELLOW_MAX_MS));
  const [formError, setFormError] = useState("");
  const settings = useQuery({
    queryKey: ["system-settings"],
    queryFn: getSystemSettings,
    enabled: management === true,
  });
  const update = useMutation({
    mutationFn: () => updateSystemSettings({
      service_quality: {
        green_max_ms: parseBoundary(greenMax) as number,
        yellow_max_ms: parseBoundary(yellowMax) as number,
      },
    }),
    onSuccess: async (next) => {
      setGreenMax(String(next.service_quality.green_max_ms));
      setYellowMax(String(next.service_quality.yellow_max_ms));
      setFormError("");
      await queryClient.invalidateQueries({ queryKey: ["system-settings"] });
      notifyToast({ title: t("System settings saved."), variant: "success" });
    },
    onError: (error) => {
      notifyToast({ title: t("Unable to save system settings."), description: t(toastErrorMessage(error)), variant: "destructive" });
    },
  });

  useEffect(() => {
    const definition = settings.data?.service_quality;
    if (!definition) return;
    setGreenMax(String(definition.green_max_ms));
    setYellowMax(String(definition.yellow_max_ms));
  }, [settings.data?.service_quality.green_max_ms, settings.data?.service_quality.yellow_max_ms]);

  if (authenticated === null || management === null) return <LoadingState rows={7} />;
  if (!authenticated) return <SessionGate />;
  if (!management) return <ManagementAdminGate />;
  if (settings.isLoading) return <LoadingState rows={7} />;
  if (settings.isError) {
    return <ErrorState error={settings.error instanceof Error ? settings.error.message : t("Unable to load system settings.")} onRetry={() => void settings.refetch()} />;
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const green = parseBoundary(greenMax);
    const yellow = parseBoundary(yellowMax);
    if (green === null || yellow === null) {
      setFormError("Enter a whole number from 0 to 300000 ms.");
      return;
    }
    if (yellow <= green) {
      setFormError("The yellow boundary must be greater than the green boundary.");
      return;
    }
    setFormError("");
    update.mutate();
  }

  const green = parseBoundary(greenMax) ?? DEFAULT_GREEN_MAX_MS;
  const yellow = parseBoundary(yellowMax) ?? DEFAULT_YELLOW_MAX_MS;
  return (
    <div className="page-stack settings-page">
      <PageHeader
        eyebrow="GOVERN"
        title="Settings"
        description="Manage global control-plane behavior."
        icon={<Settings2 size={18} />}
      />
      <Panel className="settings-panel">
        <div className="panel-heading">
          <div>
            <span className="panel-kicker">{t("SYSTEM SETTINGS")}</span>
            <h2>{t("Service quality definition")}</h2>
          </div>
          <Settings2 size={18} aria-hidden="true" />
        </div>
        <form className="settings-form" onSubmit={submit}>
          <p className="settings-description">{t("Define the latency boundaries used to classify service quality statistics.")}</p>
          <div className="settings-input-grid">
            <label>
              <span>{t("Green boundary")}</span>
              <div className="settings-number-input"><input type="number" min="0" max={MAX_LATENCY_BOUNDARY_MS} step="1" inputMode="numeric" value={greenMax} onChange={(event) => { setGreenMax(event.target.value); setFormError(""); }} /><span>{t("ms")}</span></div>
              <small>{t("Values at or below this boundary are green.")}</small>
            </label>
            <label>
              <span>{t("Yellow boundary")}</span>
              <div className="settings-number-input"><input type="number" min="1" max={MAX_LATENCY_BOUNDARY_MS} step="1" inputMode="numeric" value={yellowMax} onChange={(event) => { setYellowMax(event.target.value); setFormError(""); }} /><span>{t("ms")}</span></div>
              <small>{t("Values above green and at or below this boundary are yellow.")}</small>
            </label>
          </div>
          <div className="settings-quality-preview" aria-live="polite">
            <span className="settings-quality-preview-title">{t("Current classification")}</span>
            <div className="settings-quality-preview-row">
              <span><i className="quality-chart-swatch quality-chart-swatch-good" aria-hidden="true" />{t("Green: <= {value} ms", { value: formatNumber(green) })}</span>
              <span><i className="quality-chart-swatch quality-chart-swatch-acceptable" aria-hidden="true" />{t("Yellow: {from}-{to} ms", { from: formatNumber(green + 1), to: formatNumber(yellow) })}</span>
              <span><i className="quality-chart-swatch quality-chart-swatch-high" aria-hidden="true" />{t("Red: > {value} ms or unavailable", { value: formatNumber(yellow) })}</span>
            </div>
          </div>
          {formError ? <p className="settings-form-error" role="alert">{t(formError)}</p> : null}
          <div className="settings-form-actions">
            <Button type="submit" variant="primary" disabled={update.isPending}><Save size={15} />{update.isPending ? t("Saving...") : t("Save settings")}</Button>
          </div>
        </form>
      </Panel>
    </div>
  );
}
