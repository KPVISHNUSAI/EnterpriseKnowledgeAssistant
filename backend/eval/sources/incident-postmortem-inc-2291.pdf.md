SYNTHETIC DOCUMENT - Northwind Aerial Robotics (fictional): Postmortem INC-2291. Written for evaluation and demos; it describes no real company.

# Postmortem INC-2291: Telemetry Upload Outage

## Summary

On 14 March 2026 between 09:12 and 09:59 UTC, drones could not upload flight logs to SkyFleet. The outage lasted 47 minutes and affected 1,240 drones operated by 37 customers. No flight data was lost: drones buffered their logs locally and uploaded them after recovery.

## Root cause

The TLS certificate of the telemetry gateway expired at 09:12 UTC. The automated renewal job had been disabled on 2 February 2026 during the migration to the new load balancer and was never re-enabled. Drones rejected the expired certificate and stopped uploading.

## Timeline

- 09:12 UTC: the certificate expires and uploads start failing.
- 09:18 UTC: the synthetic upload probe fails and pages the Platform on-call engineer.
- 09:31 UTC: the on-call engineer identifies the expired certificate.
- 09:55 UTC: a new certificate is installed manually.
- 09:59 UTC: uploads recover; buffered logs finish uploading by 11:30 UTC.

## Action items

- Re-enable automated certificate renewal on the telemetry gateway. Owner: Platform team. Due: 31 March 2026.
- Add an alert that fires 21 days before any production certificate expires. Owner: Security team. Due: 15 April 2026.
- Add certificate renewal jobs to the load balancer migration checklist. Owner: Platform team. Due: 30 April 2026.

## Lessons learned

Disabling a scheduled job during a migration needs a ticket to turn it back on. Customer communication started 25 minutes after the outage began, which was too late; the status page must be updated within 15 minutes of a confirmed outage.
