import { LinkButton } from "@/components/ui/button";

export default function NotFound() {
  return (
    <main className="grid min-h-dvh place-items-center px-4 text-center">
      <div>
        <p className="text-sm font-medium text-accent-700">404</p>
        <h1 className="mt-1 text-2xl font-semibold tracking-tight">Page not found</h1>
        <p className="mt-2 text-sm text-slate-600">The page you are looking for does not exist or has moved.</p>
        <LinkButton href="/dashboard" variant="primary" className="mt-6">
          Go to dashboard
        </LinkButton>
      </div>
    </main>
  );
}
