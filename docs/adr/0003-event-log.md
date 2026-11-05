# ADR-0003 Append-only event log for replay
Status: accepted
All telemetry, alarm and control events are written to an append-only,
timestamped log. Prototype uses a file-backed log; production would use NATS
JetStream or Kafka behind the same interface.
