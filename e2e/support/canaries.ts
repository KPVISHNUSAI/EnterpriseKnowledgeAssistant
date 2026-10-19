import { stackEnv } from "../stack/config";
import { PASSWORD } from "./accounts";

/**
 * Text that exists only in passwords, secrets, document bodies and questions sent by the tests.
 * The API and the worker must never write any of it to their logs; the launcher greps the logs
 * for it when the run is over (stack/launcher.ts).
 */
export const LOG_CANARIES: string[] = [
  PASSWORD,
  stackEnv("").SECRET_KEY!,
  "eka_app_dev_password",
  "eka_owner_dev_password",
  "postgres_dev_password",
  // document bodies
  "Employees book economy class flights",
  "up to three days per week",
  "badge scanning at every door",
  "assembly point behind building C",
  "zephyrquartz",
  "acknowledges the escalation within five minutes",
  // questions and search queries
  "airspeed velocity",
  "executive bonus pool",
  "incident escalation process",
  "assembly point evacuation",
];
