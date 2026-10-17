import { Sparkles } from "lucide-react";
import { Suspense } from "react";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col items-center px-4 pt-[10vh] pb-10">
      <div className="mb-6 flex items-center gap-2.5">
        <span className="grid size-10 place-items-center rounded-xl bg-accent-600 text-white">
          <Sparkles aria-hidden className="size-5" />
        </span>
        <span className="text-lg font-semibold tracking-tight text-slate-900">Enterprise Knowledge Assistant</span>
      </div>
      <main className="w-full max-w-md rounded-xl border border-slate-200 bg-white p-6 shadow-xs sm:p-8">
        <Suspense>{children}</Suspense>
      </main>
    </div>
  );
}
