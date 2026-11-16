# Service: inventory

## Overview
Stock levels and reservations. Uses Postgres; the stock table is the busiest.

## Normal levels
Error rate below 1%. 95th percentile response time well under half a second.

## Safe actions
Restart, roll back to the previous release, or scale out by one replica. All require approval.
