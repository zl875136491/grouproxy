"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "../../components/ui";
import { usePreferences } from "../../lib/preferences";

export const ACCESS_PAGE_SIZE = 50;

export function useDebouncedValue(value: string, delay = 300) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

export function ListPagination({
  offset,
  pageCount,
  total,
  fetching,
  onPrevious,
  onNext,
}: {
  offset: number;
  pageCount: number;
  total: number;
  fetching: boolean;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const { t, formatNumber } = usePreferences();
  const from = total ? offset + 1 : 0;
  const to = Math.min(offset + pageCount, total);
  return (
    <div className="connection-history-pagination">
      <span className="toolbar-note">{t("Showing {from}-{to} of {count}", { from, to, count: formatNumber(total) })}</span>
      <div className="row-actions">
        <Button size="sm" disabled={offset === 0 || fetching} onClick={onPrevious}>
          <ChevronLeft size={15} />{t("Previous")}
        </Button>
        <Button size="sm" disabled={offset + pageCount >= total || fetching} onClick={onNext}>
          {t("Next")}<ChevronRight size={15} />
        </Button>
      </div>
    </div>
  );
}
