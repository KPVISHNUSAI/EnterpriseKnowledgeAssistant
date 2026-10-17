import { useId } from "react";

export interface Bar {
  /** Shown in the tooltip and the data table, e.g. an ISO date. */
  label: string;
  value: number;
}

/**
 * A dependency-free bar chart. The SVG has a text alternative (title + summary), every bar has a
 * tooltip, and the exact values are available as a table for screen reader and keyboard users.
 */
export function BarChart({
  bars,
  title,
  format,
  className,
}: {
  bars: Bar[];
  title: string;
  format: (value: number) => string;
  className?: string;
}) {
  const id = useId();
  const max = Math.max(0, ...bars.map((b) => b.value));
  const total = bars.reduce((sum, b) => sum + b.value, 0);
  const width = Math.max(bars.length, 1);
  const peak = bars.reduce<Bar | null>((best, bar) => (best === null || bar.value > best.value ? bar : best), null);

  return (
    <figure className={className}>
      <div className="flex items-start gap-2">
        <span className="w-12 shrink-0 text-right text-[11px] text-slate-500 tabular-nums" aria-hidden>
          {format(max)}
        </span>
        <svg
          role="img"
          aria-labelledby={`${id}-title ${id}-desc`}
          viewBox={`0 0 ${width} 100`}
          preserveAspectRatio="none"
          className="h-44 min-w-0 flex-1 border-b border-l border-slate-300 bg-slate-50/50"
        >
          <title id={`${id}-title`}>{title}</title>
          <desc id={`${id}-desc`}>
            {bars.length} days, total {format(total)}
            {peak && max > 0 ? `, highest on ${peak.label} with ${format(peak.value)}` : ", no activity"}.
          </desc>
          {bars.map((bar, index) => {
            const height = max > 0 ? (bar.value / max) * 98 : 0;
            return (
              <rect
                key={bar.label}
                x={index + 0.12}
                y={100 - height}
                width={0.76}
                height={Math.max(height, bar.value > 0 ? 0.8 : 0)}
                className="fill-accent-600 hover:fill-accent-800"
              >
                <title>{`${bar.label}: ${format(bar.value)}`}</title>
              </rect>
            );
          })}
        </svg>
      </div>
      <div className="mt-1 ml-14 flex justify-between text-[11px] text-slate-500" aria-hidden>
        <span>{bars[0]?.label}</span>
        <span>{bars.length > 1 ? bars[bars.length - 1]?.label : ""}</span>
      </div>
      <details className="mt-3 text-sm">
        <summary className="w-fit rounded-sm text-accent-700 hover:underline">View data table</summary>
        <div className="relative mt-2 max-h-60 overflow-auto rounded-lg border border-slate-200">
          <table className="w-full text-left text-xs">
            <caption className="sr-only">{title}</caption>
            <thead className="sticky top-0 bg-slate-50 text-slate-600">
              <tr>
                <th scope="col" className="px-3 py-1.5 font-medium">
                  Date
                </th>
                <th scope="col" className="px-3 py-1.5 text-right font-medium">
                  Value
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {bars.map((bar) => (
                <tr key={bar.label}>
                  <th scope="row" className="px-3 py-1 font-normal text-slate-700">
                    {bar.label}
                  </th>
                  <td className="px-3 py-1 text-right tabular-nums">{format(bar.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
