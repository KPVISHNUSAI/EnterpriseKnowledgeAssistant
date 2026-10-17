import { useId, type ComponentProps, type ReactNode } from "react";
import { cn } from "@/lib/cn";

export const controlClass =
  "block w-full rounded-md border border-slate-300 bg-white px-3 text-sm text-slate-900 placeholder:text-slate-500 disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-500 read-only:bg-slate-50 aria-invalid:border-red-500";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn(controlClass, "h-9", className)} {...props} />;
}

export function Select({ className, ...props }: ComponentProps<"select">) {
  return <select className={cn(controlClass, "h-9 pr-8", className)} {...props} />;
}

export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return <textarea className={cn(controlClass, "py-2", className)} {...props} />;
}

export function Checkbox({ className, ...props }: Omit<ComponentProps<"input">, "type">) {
  return (
    <input type="checkbox" className={cn("size-4 rounded border-slate-300 accent-accent-600", className)} {...props} />
  );
}

interface ControlProps {
  id: string;
  "aria-describedby": string | undefined;
  "aria-invalid": true | undefined;
}

/**
 * Label + control + hint + error, wired together for assistive tech:
 *   <Field label="Email" error={errors.email}>{(p) => <Input type="email" {...p} />}</Field>
 */
export function Field({
  label,
  hint,
  error,
  hideLabel,
  className,
  children,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  hideLabel?: boolean;
  className?: string;
  children: (props: ControlProps) => ReactNode;
}) {
  const id = useId();
  const showHint = Boolean(hint) && !error; // the error usually restates the hint
  const describedBy = [showHint ? `${id}-hint` : null, error ? `${id}-error` : null].filter(Boolean).join(" ");
  return (
    <div className={className}>
      <label htmlFor={id} className={cn("mb-1 block text-sm font-medium text-slate-700", hideLabel && "sr-only")}>
        {label}
      </label>
      {children({
        id,
        "aria-describedby": describedBy || undefined,
        "aria-invalid": error ? true : undefined,
      })}
      {showHint ? (
        <p id={`${id}-hint`} className="mt-1 text-xs text-slate-500">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} role="alert" className="mt-1 text-xs font-medium text-red-600">
          {error}
        </p>
      ) : null}
    </div>
  );
}
