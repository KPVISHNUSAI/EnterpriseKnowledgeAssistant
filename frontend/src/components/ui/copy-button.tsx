"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/cn";
import { Button, IconButton } from "./button";

export function CopyButton({
  text,
  label = "Copy",
  iconOnly,
  className,
  variant = "ghost",
}: {
  text: string | (() => string);
  label?: string;
  iconOnly?: boolean;
  className?: string;
  variant?: "ghost" | "secondary";
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  async function onClick() {
    const ok = await copyText(typeof text === "function" ? text() : text);
    setCopied(ok);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1800);
  }

  const icon = copied ? (
    <Check aria-hidden className="size-4 text-emerald-600" />
  ) : (
    <Copy aria-hidden className="size-4" />
  );
  return iconOnly ? (
    <IconButton label={copied ? "Copied" : label} onClick={onClick} className={cn("size-6", className)}>
      {icon}
    </IconButton>
  ) : (
    <Button size="sm" variant={variant} onClick={onClick} className={className}>
      {icon}
      <span aria-live="polite">{copied ? "Copied" : label}</span>
    </Button>
  );
}
