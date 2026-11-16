# High latency triage

## Symptoms
95th percentile response time above 1 second. Callers of the slow service also become slow, and may log upstream timeouts.

## Checks
- Find which service slowed down first; latency flows upward from the slowest callee to its callers.
- For a service with a database, check database query time and busy connections.
- Check CPU use; a saturated process slows every request.
- A caller that is slow only because its callee is slow is a symptom, not the cause.

## Likely causes
Slow database queries, an exhausted connection pool, CPU saturation, or a downstream service that is overloaded.

## Fix
Treat the underlying cause (see the specific runbooks). Do not restart the alerting service if the slowness comes from further down the call chain.
