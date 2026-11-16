# Service: gateway

## Overview
Public entry point. Calls orders. No database.

## Normal levels
Error rate below 1%. 95th percentile response time well under half a second.

## Safe actions
Restart, roll back to the previous release, or scale out by one replica. All require approval.
