"use client";

import { CalendarRange, ChevronDown, Search, X } from "lucide-react";
import type { Dispatch, ReactNode, SetStateAction } from "react";
import { usePreferences } from "../lib/preferences";
import { IconButton } from "./ui";

export type TimeRange = "24h" | "7d" | "30d" | "all";

export type FilterOption = { value: string; label: string };

export function FilterSelect({
  label,
  value,
  setValue,
  options,
  icon,
}: {
  label: string;
  value: string;
  setValue: (value: string) => void;
  options: FilterOption[];
  icon?: ReactNode;
}) {
  const { t } = usePreferences();
  return (
    <label className="select-control list-filter-select">
      {icon}
      <span>{t(label)}</span>
      <span className="select-trigger">
        <select value={value} aria-label={t(label)} onChange={(event) => setValue(event.target.value)}>
          {options.map((option) => <option value={option.value} key={option.value}>{t(option.label)}</option>)}
        </select>
        <ChevronDown size={14} aria-hidden="true" />
      </span>
    </label>
  );
}

export function timeRangeStart(value: TimeRange): string | undefined {
  if (value === "all") return undefined;
  const hours = value === "24h" ? 24 : value === "7d" ? 24 * 7 : 24 * 30;
  return new Date(Date.now() - hours * 60 * 60 * 1000).toISOString();
}

export function ListFilters({
  timeRange,
  setTimeRange,
  selects = [],
  search,
  setSearch,
  searchPlaceholder,
}: {
  timeRange: TimeRange;
  setTimeRange: Dispatch<SetStateAction<TimeRange>>;
  selects?: Array<{ label: string; value: string; setValue: (value: string) => void; options: FilterOption[] }>;
  search?: string;
  setSearch?: (value: string) => void;
  searchPlaceholder?: string;
}) {
  const { t } = usePreferences();
  return (
    <div className="list-filters" role="search">
      {search !== undefined && setSearch ? (
        <label className="filter-search">
          <Search size={15} aria-hidden="true" />
          <span className="sr-only">{t("Search")}</span>
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder={t(searchPlaceholder || "Search")}/>
          {search ? <IconButton label="Clear search" tooltip={false} onClick={() => setSearch("")}><X size={14} /></IconButton> : null}
        </label>
      ) : null}
      {selects.map((select) => <FilterSelect key={select.label} label={select.label} value={select.value} setValue={select.setValue} options={select.options} />)}
      <FilterSelect
        label="Time range"
        value={timeRange}
        setValue={(value) => setTimeRange(value as TimeRange)}
        options={[
          { value: "24h", label: "Last 24 hours" },
          { value: "7d", label: "Last 7 days" },
          { value: "30d", label: "Last 30 days" },
          { value: "all", label: "All time" },
        ]}
        icon={<CalendarRange size={15} aria-hidden="true" />}
      />
    </div>
  );
}
