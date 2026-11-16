# Disk space exhausted

## Symptoms
Free disk space falls towards zero; writes fail with "No space left on device"; the health check fails.

## Checks
- Query disk_free over time: a steady fall to zero confirms it.
- Search logs for Errno 28 / No space left on device.

## Likely causes
Logs or queue files filling the volume (resource exhaustion).

## Fix
Restart the service, which clears its temporary queue files, then add log rotation.
