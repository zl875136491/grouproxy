export function cn(...values: Array<string | false | null | undefined>) {
  return values.filter(Boolean).join(" ");
}

export function label(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function activeLocale() {
  if (typeof document === "undefined") return "zh-CN";
  return document.documentElement.lang || "zh-CN";
}

export function shortHash(value: string, length = 12) {
  if (!value) return "-";
  return value.length <= length ? value : `${value.slice(0, length)}...`;
}

export function formatDate(value: string | null | undefined, withTime = true, locale = activeLocale()) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
  }).format(date);
}

export function formatDurationUntil(value: string, locale = activeLocale()) {
  const milliseconds = new Date(value).getTime() - Date.now();
  if (Number.isNaN(milliseconds)) return "-";
  const minutes = Math.round(milliseconds / 60_000);
  if (minutes < 0) return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(0, "minute");
  if (minutes < 60) return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(minutes, "minute");
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(hours, "hour");
  return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(Math.floor(hours / 24), "day");
}
