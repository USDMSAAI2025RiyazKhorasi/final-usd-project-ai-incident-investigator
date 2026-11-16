# Cascading failures

## Symptoms
An alert fires on an edge service such as the gateway, but the gateway itself is healthy apart from timeouts to its callees. Upstream timeout errors appear at each level of the call chain.

## Checks
- Use the service map to walk down from the alerting service.
- At each level, check whether the service has its own problem (CPU, memory, database, dependency) or only timeouts to the service below.
- The cause is the deepest service with its own problem; everything above it is a symptom.

## Likely causes
An overloaded or saturated service deep in the call chain (resource exhaustion).

## Fix
Restart the deepest affected service. Restarting the gateway does not help.
