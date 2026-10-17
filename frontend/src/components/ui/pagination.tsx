import { ChevronLeft, ChevronRight } from "lucide-react";
import { formatNumber } from "@/lib/format";
import { Button } from "./button";
import { Select } from "./form";

export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  pageSizes = [10, 20, 50, 100],
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange?: (size: number) => void;
  pageSizes?: number[];
}) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  return (
    <nav aria-label="Pagination" className="flex flex-wrap items-center justify-between gap-3 text-sm text-slate-600">
      <p aria-live="polite">
        {total === 0 ? "No results" : `${formatNumber(from)}-${formatNumber(to)} of ${formatNumber(total)}`}
      </p>
      <div className="flex items-center gap-2">
        {onPageSizeChange ? (
          <label className="flex items-center gap-2">
            <span className="sr-only sm:not-sr-only">Rows</span>
            <Select
              className="h-8 w-20 py-0 text-[13px]"
              value={pageSize}
              onChange={(event) => onPageSizeChange(Number(event.target.value))}
            >
              {pageSizes.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </Select>
          </label>
        ) : null}
        <Button
          size="sm"
          variant="secondary"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
          aria-label="Previous page"
        >
          <ChevronLeft aria-hidden className="size-4" />
        </Button>
        <span className="min-w-20 text-center">
          Page {page} of {pages}
        </span>
        <Button
          size="sm"
          variant="secondary"
          disabled={page >= pages}
          onClick={() => onPageChange(page + 1)}
          aria-label="Next page"
        >
          <ChevronRight aria-hidden className="size-4" />
        </Button>
      </div>
    </nav>
  );
}
