/**
 * The ingestion worker's process group, started by stack/launcher.ts. Suspending it holds
 * uploaded documents in "queued" for as long as a test needs to look at that state; the
 * launcher resumes it again before stopping it. Other tests only wait a little longer.
 */
const group = () => -Number(process.env.E2E_WORKER_PID);

export const pauseWorker = () => process.kill(group(), "SIGSTOP");
export const resumeWorker = () => process.kill(group(), "SIGCONT");
