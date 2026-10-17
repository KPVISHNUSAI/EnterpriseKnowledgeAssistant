"use client";

import * as AlertDialog from "@radix-ui/react-alert-dialog";
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { buttonClass } from "./button";
import { useReturnFocus } from "./dialog";

export interface ConfirmOptions {
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
}

type Confirm = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<Confirm | null>(null);

/** `const confirm = useConfirm(); if (await confirm({...})) doIt();` */
export function useConfirm(): Confirm {
  const confirm = useContext(ConfirmContext);
  if (!confirm) throw new Error("useConfirm must be used inside <ConfirmProvider>");
  return confirm;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((value: boolean) => void) | null>(null);
  const returnFocus = useReturnFocus();

  const confirm = useCallback<Confirm>((next) => {
    resolver.current?.(false);
    setOptions(next);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  function settle(value: boolean) {
    resolver.current?.(value);
    resolver.current = null;
    setOptions(null);
  }

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <AlertDialog.Root open={options !== null} onOpenChange={(open) => !open && settle(false)}>
        <AlertDialog.Portal>
          <AlertDialog.Overlay className="fixed inset-0 z-[60] grid animate-fade-in place-items-center overflow-y-auto bg-slate-900/40 p-4">
            <AlertDialog.Content
              {...returnFocus}
              className="w-full max-w-md animate-pop-in rounded-lg bg-white p-5 shadow-xl"
            >
              <AlertDialog.Title className="text-base font-semibold text-slate-900">{options?.title}</AlertDialog.Title>
              <AlertDialog.Description asChild>
                <div className="mt-2 text-sm text-slate-600">{options?.description}</div>
              </AlertDialog.Description>
              <div className="mt-5 flex justify-end gap-2">
                <AlertDialog.Cancel className={buttonClass("secondary")}>
                  {options?.cancelLabel ?? "Cancel"}
                </AlertDialog.Cancel>
                <AlertDialog.Action
                  className={buttonClass(options?.destructive ? "danger" : "primary")}
                  onClick={() => settle(true)}
                  data-testid="confirm-action"
                >
                  {options?.confirmLabel ?? "Confirm"}
                </AlertDialog.Action>
              </div>
            </AlertDialog.Content>
          </AlertDialog.Overlay>
        </AlertDialog.Portal>
      </AlertDialog.Root>
    </ConfirmContext.Provider>
  );
}
