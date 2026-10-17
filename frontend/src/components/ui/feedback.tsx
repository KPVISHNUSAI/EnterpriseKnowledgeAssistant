import type { ComponentProps, ReactNode } from "react";
import { AlertCircle, AlertTriangle, CheckCircle2, Info, Loader2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { ApiError, errorMessage, errorRequestId } from "@/lib/api/errors";
import { Button } from "./button";
import { CopyButton } from "./copy-button";

export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <span role="status" className="inline-flex items-center">
      <Loader2 aria-hidden className={cn("size-4 animate-spin text-slate-500", className)} />
      <span className="sr-only">{label ?? "Loading"}</span>
    </span>
  );
}

export function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return <div aria-hidden className={cn("animate-pulse rounded-md bg-slate-200/70", className)} {...props} />;
}

export function SkeletonRows({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-3", className)} role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-10 w-full" />
      ))}
      <span className="sr-only">Loading</span>
    </div>
  );
}

type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "accent";

const badgeTones: Record<Tone, string> = {
  neutral: "bg-slate-100 text-slate-700 ring-slate-200",
  info: "bg-sky-50 text-sky-800 ring-sky-200",
  success: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  warning: "bg-amber-50 text-amber-900 ring-amber-200",
  danger: "bg-red-50 text-red-800 ring-red-200",
  accent: "bg-accent-50 text-accent-800 ring-accent-200",
};

export function Badge({ tone = "neutral", className, ...props }: ComponentProps<"span"> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
        badgeTones[tone],
        className,
      )}
      {...props}
    />
  );
}

const alertStyles: Record<Exclude<Tone, "neutral" | "accent">, { box: string; icon: ReactNode }> = {
  info: { box: "border-sky-200 bg-sky-50 text-sky-900", icon: <Info aria-hidden className="size-4 text-sky-600" /> },
  success: {
    box: "border-emerald-200 bg-emerald-50 text-emerald-900",
    icon: <CheckCircle2 aria-hidden className="size-4 text-emerald-600" />,
  },
  warning: {
    box: "border-amber-200 bg-amber-50 text-amber-900",
    icon: <AlertTriangle aria-hidden className="size-4 text-amber-600" />,
  },
  danger: {
    box: "border-red-200 bg-red-50 text-red-900",
    icon: <AlertCircle aria-hidden className="size-4 text-red-600" />,
  },
};

export function Alert({
  tone = "info",
  title,
  children,
  className,
  ...props
}: Omit<ComponentProps<"div">, "title"> & {
  tone?: keyof typeof alertStyles;
  title?: ReactNode;
}) {
  const style = alertStyles[tone];
  return (
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={cn("flex gap-3 rounded-lg border p-3 text-sm", style.box, className)}
      {...props}
    >
      <span className="mt-0.5 shrink-0">{style.icon}</span>
      <div className="min-w-0 flex-1">
        {title ? <p className="font-medium">{title}</p> : null}
        {children ? <div className={title ? "mt-0.5" : undefined}>{children}</div> : null}
      </div>
    </div>
  );
}

/** An API/network error with the server's request id kept visible for support. */
export function ErrorNotice({
  error,
  title = "Something went wrong",
  onRetry,
  className,
  testId = "error-notice",
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
  testId?: string;
}) {
  const requestId = errorRequestId(error);
  const status = error instanceof ApiError && error.status > 0 ? error.status : null;
  return (
    <Alert tone="danger" title={title} className={className} data-testid={testId}>
      <p>{errorMessage(error)}</p>
      {requestId || status ? (
        <p className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-red-800/80">
          {status ? <span>HTTP {status}</span> : null}
          {requestId ? (
            <>
              <span>
                Request ID: <code className="font-mono select-all">{requestId}</code>
              </span>
              <CopyButton text={requestId} label="Copy request ID" iconOnly />
            </>
          ) : null}
        </p>
      ) : null}
      {onRetry ? (
        <Button size="sm" variant="secondary" className="mt-2" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </Alert>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: ReactNode;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center rounded-lg border border-dashed border-slate-300 bg-white px-6 py-12 text-center",
        className,
      )}
    >
      {icon ? <div className="mb-3 text-slate-400 [&>svg]:size-8">{icon}</div> : null}
      <h3 className="text-base font-medium text-slate-900">{title}</h3>
      {description ? <p className="mt-1 max-w-md text-sm text-slate-600">{description}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <title>{`${title} | Knowledge Assistant`}</title>
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-slate-900">{title}</h1>
        {description ? <p className="mt-1 text-sm text-slate-600">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function Card({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("rounded-lg border border-slate-200 bg-white shadow-xs", className)} {...props} />;
}

export function CardHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
        {description ? <p className="text-xs text-slate-500">{description}</p> : null}
      </div>
      {actions}
    </div>
  );
}
