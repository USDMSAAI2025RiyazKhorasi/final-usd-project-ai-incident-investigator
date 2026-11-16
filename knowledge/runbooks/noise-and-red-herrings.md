# Telling noise from the cause

## Symptoms
More than one thing looks wrong at once: background errors on another service, a release that happened at about the same time, a background job raising CPU.

## Checks
- Did the anomaly start before the incident and continue unchanged? Then it is background noise.
- Is the anomalous service on the call path to the alerting service? If not, it cannot explain the alert.
- A release only matters if its service is where the problem begins and its diff could plausibly cause it.
- Prefer evidence that both explains the symptom and started first.

## Fix
Do not act on a red herring; act only on the service where the problem originates.
