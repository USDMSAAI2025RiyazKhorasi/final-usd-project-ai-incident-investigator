# Memory growth and OOM kills

## Symptoms
Container memory climbs steadily towards its limit, garbage-collection pauses grow, and the process is eventually killed with OOMKilled and restarted. Request counters reset after each restart.

## Checks
- Plot memory over the last 15 minutes: a steady climb that resets after a restart is a leak.
- Search logs for OOMKilled and long GC pauses.
- Check whether a release coincides with the start of the climb.

## Likely causes
A memory leak in the service (resource exhaustion).

## Fix
Restart the service to recover immediately, then open a bug for the leak. If a recent release introduced it, roll back instead.
