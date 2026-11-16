# Slow database queries

## Symptoms
Database query time (95th percentile) rises from tens of milliseconds to seconds; the service's response time follows. Logs show "slow query" warnings.

## Checks
- Query db_query_p95 for each service that uses the database.
- Search logs for slow query messages and note which table and statement appear.
- Check whether busy connections are also rising (queries holding connections longer).

## Likely causes
A missing index or a changed query plan, often after a schema or code release (database performance).

## Fix
Roll back the release that changed the query. If no release is involved, escalate to the database owner.
