"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { useRef, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { IconButton } from "./button";

/**
 * Radix only restores focus to a <Dialog.Trigger>. Ours are opened from state, so remember the
 * element that had focus when the dialog opened and return to it on close.
 */
export function useReturnFocus() {
  const opener = useRef<HTMLElement | null>(null);
  return {
    onOpenAutoFocus: () => {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    },
    onCloseAutoFocus: (event: Event) => {
      event.preventDefault();
      opener.current?.focus({ preventScroll: true });
    },
  };
}

const sizes = { sm: "max-w-sm", md: "max-w-lg", lg: "max-w-2xl", xl: "max-w-4xl" };

/** Accessible modal (focus trap, Escape closes, focus returns to the trigger). */
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  size = "md",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: keyof typeof sizes;
}) {
  const returnFocus = useReturnFocus();
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 grid animate-fade-in place-items-center overflow-y-auto bg-slate-900/40 p-4">
          <Dialog.Content
            {...returnFocus}
            {...(description ? {} : { "aria-describedby": undefined })}
            className={cn("w-full animate-pop-in rounded-lg bg-white shadow-xl", sizes[size])}
          >
            <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
              <div className="min-w-0">
                <Dialog.Title className="text-base font-semibold text-slate-900">{title}</Dialog.Title>
                {description ? (
                  <Dialog.Description className="mt-1 text-sm text-slate-600">{description}</Dialog.Description>
                ) : null}
              </div>
              <Dialog.Close asChild>
                <IconButton label="Close" className="-mr-2 -mt-1">
                  <X aria-hidden className="size-4" />
                </IconButton>
              </Dialog.Close>
            </div>
            {children ? <div className="px-5 py-4">{children}</div> : null}
            {footer ? (
              <div className="flex flex-wrap justify-end gap-2 border-t border-slate-200 px-5 py-3">{footer}</div>
            ) : null}
          </Dialog.Content>
        </Dialog.Overlay>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** Side sheet used for the mobile navigation and panels that are inline on wide screens. */
export function Drawer({
  open,
  onOpenChange,
  title,
  side = "left",
  children,
  className,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  side?: "left" | "right";
  children: ReactNode;
  className?: string;
}) {
  const returnFocus = useReturnFocus();
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 animate-fade-in bg-slate-900/40" />
        <Dialog.Content
          {...returnFocus}
          aria-describedby={undefined}
          className={cn(
            "fixed inset-y-0 z-50 flex w-[min(22rem,calc(100vw-2rem))] flex-col bg-white shadow-xl",
            side === "left" ? "left-0 animate-slide-in-left" : "right-0 animate-slide-in-right",
            className,
          )}
        >
          <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
            <Dialog.Title className="text-sm font-semibold text-slate-900">{title}</Dialog.Title>
            <Dialog.Close asChild>
              <IconButton label="Close">
                <X aria-hidden className="size-4" />
              </IconButton>
            </Dialog.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
