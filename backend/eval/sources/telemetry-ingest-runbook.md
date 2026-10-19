SYNTHETIC DOCUMENT - Northwind Aerial Robotics (fictional): Runbook: Telemetry Ingest Service. Written for evaluation and demos; it describes no real company.

# Runbook: Telemetry Ingest Service

## Overview

The telemetry-ingest service receives flight logs from drones over MQTT and writes them to the `flight-telemetry` Kafka topic, which has 12 partitions. It runs in the `flight-data` namespace of the production Kubernetes cluster and is owned by the Platform team.

## Alerts

`TelemetryLagHigh` fires when consumer lag stays above 50,000 messages for 10 minutes. `TelemetryIngestDown` fires when fewer than 2 pods are ready. Both alerts page the Platform on-call engineer.

## Restarting the service

Restart the deployment and wait until the rollout has finished:

```bash
kubectl -n flight-data rollout restart deployment/telemetry-ingest
kubectl -n flight-data rollout status deployment/telemetry-ingest --timeout=300s
```

A restart drains in-flight batches first, so no telemetry is lost.

## Configuration

The service reads `config/ingest.yaml` at start-up:

```yaml
ingest:
  max_batch_size: 500
  flush_interval_ms: 2000
  queue_high_watermark: 80000
  mqtt:
    keepalive_seconds: 45
    qos: 1
```

Lowering `max_batch_size` reduces memory use but increases the load on Kafka.

## Scaling

Scale out when lag keeps growing after a restart:

```bash
kubectl -n flight-data scale deployment/telemetry-ingest --replicas=6
```

Never run more than 12 replicas: the topic has 12 partitions, so extra consumers sit idle and slow down rebalancing.

## Escalation

If lag is still growing 30 minutes after two restarts, escalate to the Platform engineering lead and open an incident in the incident tracker.
