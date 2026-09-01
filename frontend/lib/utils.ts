export function cn(...values: Array<string | false | null | undefined>) {
  return values.filter(Boolean).join(" ");
}

export function label(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

export function shortHash(value: string, length = 12) {
  if (!value) return "-";
  return value.length <= length ? value : `${value.slice(0, length)}...`;
}

export type Formatters = {
  formatDate: (value: string | null | undefined, withTime?: boolean) => string;
  formatDurationUntil: (value: string | null | undefined) => string;
  formatNumber: (value: number | null | undefined, options?: Intl.NumberFormatOptions) => string;
  formatPercent: (value: number | null | undefined) => string;
  formatBytes: (value: number | null | undefined) => string;
  formatDuration: (value: number | null | undefined) => string;
};

// API responses stay canonical (ISO dates and numeric values). These functions
// are intentionally bound to an explicit locale so a React locale change can
// update presentation without touching the query cache or making a request.
export function createFormatters(locale: string): Formatters {
  const dateFormatter = new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
  const dateTimeFormatter = new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const relativeTimeFormatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const numberFormatters = new Map<string, Intl.NumberFormat>();

  function numberFormatter(options?: Intl.NumberFormatOptions) {
    const key = JSON.stringify(options || {});
    let formatter = numberFormatters.get(key);
    if (!formatter) {
      formatter = new Intl.NumberFormat(locale, options);
      numberFormatters.set(key, formatter);
    }
    return formatter;
  }

  const formatNumber = (value: number | null | undefined, options?: Intl.NumberFormatOptions) => {
    if (value === null || value === undefined || !Number.isFinite(value)) return "-";
    return numberFormatter(options).format(value);
  };

  return {
    formatDate(value, withTime = true) {
      if (!value) return "-";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "-";
      return (withTime ? dateTimeFormatter : dateFormatter).format(date);
    },
    formatDurationUntil(value) {
      if (!value) return "-";
      const milliseconds = new Date(value).getTime() - Date.now();
      if (Number.isNaN(milliseconds)) return "-";
      const minutes = Math.round(milliseconds / 60_000);
      if (minutes < 0) return relativeTimeFormatter.format(0, "minute");
      if (minutes < 60) return relativeTimeFormatter.format(minutes, "minute");
      const hours = Math.floor(minutes / 60);
      if (hours < 48) return relativeTimeFormatter.format(hours, "hour");
      return relativeTimeFormatter.format(Math.floor(hours / 24), "day");
    },
    formatNumber,
    // API progress fields use the human-facing 0..100 scale. Intl's percent
    // formatter expects a ratio, so normalize once at this boundary.
    formatPercent(value) {
      if (value === null || value === undefined || !Number.isFinite(value)) return "-";
      return formatNumber(value / 100, {
        style: "percent",
        maximumFractionDigits: 0,
      });
    },
    formatBytes(value) {
      if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return "-";
      const units: Intl.NumberFormatOptions["unit"][] = ["byte", "kilobyte", "megabyte", "gigabyte", "terabyte"];
      let unitIndex = 0;
      let amount = value;
      while (amount >= 1000 && unitIndex < units.length - 1) {
        amount /= 1000;
        unitIndex += 1;
      }
      return numberFormatter({
        style: "unit",
        unit: units[unitIndex],
        unitDisplay: "short",
        maximumFractionDigits: unitIndex === 0 ? 0 : 1,
      }).format(amount);
    },
    formatDuration(value) {
      if (value === null || value === undefined || !Number.isFinite(value) || value < 0) return "-";
      if (value < 1000) return formatNumber(value, { style: "unit", unit: "millisecond", unitDisplay: "short", maximumFractionDigits: 0 });
      if (value < 60_000) return formatNumber(value / 1000, { style: "unit", unit: "second", unitDisplay: "short", maximumFractionDigits: 1 });
      if (value < 3_600_000) return formatNumber(value / 60_000, { style: "unit", unit: "minute", unitDisplay: "short", maximumFractionDigits: 1 });
      return formatNumber(value / 3_600_000, { style: "unit", unit: "hour", unitDisplay: "short", maximumFractionDigits: 1 });
    },
  };
}
