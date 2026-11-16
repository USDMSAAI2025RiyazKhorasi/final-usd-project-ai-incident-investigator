# Authentication failures with an external API

## Symptoms
Calls to an external API (for example the bank) fail with 401 Unauthorized; error rate rises at once.

## Checks
- Search logs for 401 Unauthorized and messages about expired or rejected tokens.
- Check recent releases for changes to secret or credential references.

## Likely causes
An expired or wrong credential (credential failure), often after a credential rotation release.

## Fix
Roll back the release that changed the credential reference, then rotate the secret properly.
