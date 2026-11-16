# Service: notifications

## Overview
Email and SMS. Uses Redis as a queue and writes queue files to /var/lib/notifications.

## Normal levels
Error rate below 1%. 95th percentile response time well under half a second.

## Safe actions
Restart, roll back to the previous release, or scale out by one replica. All require approval.
