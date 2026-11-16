# Error spike after a release

## Symptoms
Server error rate rises within a few minutes of a release on the same service. Callers retry and may report 503s or timeouts of their own.

## Checks
- List releases in the last hour and compare their times with when errors started.
- Read the commit diff: configuration changes (timeouts, URLs, feature flags, secrets references) and handler code changes are the usual suspects.
- Search the service's error logs for new patterns that first appear after the release: exceptions with a traceback point to code; "timeout after" with an unusually small value points to configuration.
- Check whether a release on a different service happened at the same time; a release only matters if its service is where the errors begin.

## Likely causes
- Configuration change: a timeout or connection setting made too strict, a wrong endpoint, a credential reference pointing at an old version.
- Code change: an unhandled exception on a common path.

## Fix
Roll back the service to its previous release. Rollback is the safest first action when the errors start right after a release.

## Rollback / escalation
If errors continue after rollback, the release was not the cause; reopen the investigation.
