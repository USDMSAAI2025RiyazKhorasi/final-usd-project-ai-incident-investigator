# CPU saturation

## Symptoms
CPU use near 100% of the allocation, response time rising, warnings about a blocked event loop or delayed worker heartbeats.

## Checks
- Query cpu for the service and compare with its normal level.
- Check request rate: if traffic is normal, the extra CPU comes from the service itself.
- Background jobs (compaction, batch work) can briefly raise CPU without causing user-facing slowness; check whether latency moved too.

## Likely causes
A runaway loop or expensive code path (resource exhaustion).

## Fix
Restart the service to recover; investigate the hot path afterwards.
