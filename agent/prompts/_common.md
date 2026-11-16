Services: gateway, orders, payments, inventory, notifications.
Root-cause categories (choose exactly one per hypothesis):
- resource_exhaustion: the service ran out of a resource it owns (memory, CPU, database connections, disk).
- database_performance: the service's database queries became slow.
- config_change: a configuration change in a release broke the service.
- code_change: a code change in a release broke the service.
- dependency_unavailable: something the service depends on (database, cache, external API) cannot be reached.
- credential_failure: the service's credentials for an external API are rejected.

How problems spread: when a service fails or slows down, every service that calls it sees errors, timeouts and slowness too. The alerting service is often only a symptom. A service whose only problem is errors or timeouts from a service it calls is not the root cause. Anomalies that started well before the incident, or on services off the call path to the alerting service, are usually unrelated noise.
