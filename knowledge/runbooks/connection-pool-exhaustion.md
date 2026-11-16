# Database connection pool exhaustion

## Symptoms
Busy database connections climb to the pool limit of 20. Requests wait for a connection, latency jumps, and logs show "pool timeout" errors.

## Checks
- Query db_pool_in_use: a value pinned at 20 confirms exhaustion.
- Compare with database query time: if queries are fast but the pool is full, connections are being held or leaked.
- Check request rate for a traffic surge.

## Likely causes
Connections held too long or not returned to the pool (resource exhaustion).

## Fix
Scale the service out by one replica to add capacity, then investigate the connection handling.
