# Service: orders

## Overview
Order workflow. Calls payments, inventory and notifications. Uses Postgres with a connection pool of 20.

## Normal levels
Error rate below 1%. 95th percentile response time well under half a second.

## Safe actions
Restart, roll back to the previous release, or scale out by one replica. All require approval.
