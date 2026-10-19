import { defineConfig, devices } from "@playwright/test";
import { FRONTEND_ORIGIN } from "./stack/config";

// The limits are generous on purpose: the stack is one API process, one worker and a browser sharing
// whatever machine runs the suite, and a laptop busy with other work is several times slower than CI.
// A slow run should pass; a broken one fails by assertion, not by timeout.
export default defineConfig({
  testDir: "./tests",
  globalSetup: "./stack/launcher.ts",
  // Every test registers its own organization, so tests (and files) are independent. The two
  // workers share one API, one ingestion worker and one database.
  workers: 2,
  retries: process.env.CI ? 1 : 0,
  timeout: 240_000,
  expect: { timeout: 45_000 },
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: FRONTEND_ORIGIN,
    locale: "en-US",
    timezoneId: "UTC",
    actionTimeout: 45_000,
    navigationTimeout: 60_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      // Wide enough for the chat page to keep the conversation list next to the source panel.
      use: { ...devices["Desktop Chrome"], viewport: { width: 1600, height: 1000 } },
    },
  ],
});
