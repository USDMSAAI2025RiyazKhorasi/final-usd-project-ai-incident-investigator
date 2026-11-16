# Dependency outage (cache, database, external API)

## Symptoms
A service reports its dependency as unreachable; logs show "Connection refused" or connection errors; error rate rises sharply and almost immediately.

## Checks
- Query dependency_up for the service: 0 means the dependency cannot be reached.
- Search logs for connection refused or connection errors naming the dependency host and port.
- Other services using the same dependency will fail too.

## Likely causes
The dependency itself is down (unavailable dependency).

## Fix
Restart the dependency (for example the Redis cache). Restarting the calling service does not help.
