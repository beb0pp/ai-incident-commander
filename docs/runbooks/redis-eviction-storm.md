# Redis Eviction Storm

Applies when an ElastiCache Redis cluster begins evicting keys under memory
pressure, causing a cache-miss cascade onto the origin.

## Symptoms

- `Evictions` is non-zero and rising; `DatabaseMemoryUsagePercentage` is near 100.
- Origin services see a sudden read-volume increase without a traffic increase.
- Latency rises across every service sharing the cluster, not just one.
- `connected_clients` may spike as callers retry.

## Diagnosis

- Read the cluster's memory usage, eviction count, and connected client count.
- Identify what grew: a new key pattern, a TTL that stopped being set, or a
  payload that got larger. `redis-cli --bigkeys` on a replica is safe to run.
- Confirm the maxmemory policy. `noeviction` produces write errors instead of
  evictions; `allkeys-lru` produces this symptom.
- Check whether the origin can absorb a full cache miss. If it cannot, the cache
  is load-bearing and the incident is a capacity problem, not a cache problem.

## Mitigation

- Shed the largest low-value key space first, if one is identifiable.
- Restore TTLs on any key pattern that lost them.
- Scale the node type up if eviction is driven by genuine working-set growth.
- Do not flush the cache. A cold cache moves the entire working set onto the
  origin at once and reliably turns a degradation into an outage.

## Rollback

- Reverting a node-type change requires a maintenance window; plan it outside the
  incident.

## Escalation

- Escalate to the owning team if eviction continues after the working set is
  reduced, which indicates unbounded key growth in application code.
