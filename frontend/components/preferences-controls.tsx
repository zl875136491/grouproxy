"use client";

import { Globe2, Moon, Sun } from "lucide-react";
import { usePreferences, type Locale } from "../lib/preferences";
import { cn } from "../lib/utils";
import { IconButton } from "./ui";

const localeOptions: Array<{ value: Locale; label: string }> = [
  { value: "zh-CN", label: "中文" },
  { value: "en", label: "English" },
  { value: "es", label: "Español" },
];

export function PreferencesControls({ className }: { className?: string }) {
  const { locale, setLocale, theme, setTheme, t } = usePreferences();
  const dark = theme === "dark";

  return (
    <div className={cn("preferences-controls", className)}>
      <label className="locale-control">
        <Globe2 size={15} aria-hidden="true" />
        <span className="sr-only">{t("Language")}</span>
        <select value={locale} onChange={(event) => setLocale(event.target.value as Locale)} aria-label={t("Language")}>
          {localeOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
      </label>
      <IconButton label={t(dark ? "Switch to light mode" : "Switch to dark mode")} onClick={() => setTheme(dark ? "light" : "dark")}>
        {dark ? <Sun size={16} /> : <Moon size={16} />}
      </IconButton>
    </div>
  );
}
