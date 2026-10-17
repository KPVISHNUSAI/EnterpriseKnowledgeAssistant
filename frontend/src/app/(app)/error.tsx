"use client";

import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/feedback";

// Last-resort boundary for rendering bugs; API failures are handled where they happen.
export default function AppError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="mx-auto max-w-lg py-16">
      <Alert tone="danger" title="This page hit an unexpected error">
        <p>Reload the page or try again. If it keeps happening, tell your administrator.</p>
        {error.digest ? (
          <p className="mt-1 text-xs">
            Reference: <code className="font-mono select-all">{error.digest}</code>
          </p>
        ) : null}
        <Button className="mt-3" size="sm" onClick={reset}>
          Try again
        </Button>
      </Alert>
    </div>
  );
}
