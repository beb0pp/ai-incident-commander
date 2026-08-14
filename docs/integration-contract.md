# Integration contract

What this platform needs from an environment in order to investigate well, and
what commonly provides it.

Everything here is stated as a **capability**, never as a vendor API. That is
deliberate: the moment a capability is defined as "SQS", the platform stops
being a product and becomes one company's integration. `describe_queue` has to
mean "tell me about the backlog", whether the backlog lives in SQS, Kafka,
RabbitMQ, or a Redis stream.

The `InfrastructureClient` protocol is the code expression of this document.
Adding an environment means implementing capabilities, not editing agents.

---

## What the platform consumes

### Signals — the input to an investigation

One normalized shape, four kinds. Whatever emits telemetry in your environment
gets flattened into this before it reaches an agent.

| Kind | What it carries | Commonly comes from |
|---|---|---|
| `log` | A line, with level and any structured fields as labels | CloudWatch Logs, Loki, Elasticsearch, Datadog Logs, Splunk |
| `metric` | A named value at a timestamp | CloudWatch, Prometheus, Datadog, New Relic |
| `event` | Something happened — a deploy, a config change, a failover | ECS/Kubernetes events, CI systems, CloudTrail, audit logs |
| `trace` | A span or a latency measurement | OpenTelemetry, X-Ray, Datadog APM, Jaeger |

The one rule that is not negotiable: **timestamps carry a timezone**. A
timeline that is subtly wrong is worse than one that is obviously missing.

### The trigger — when to start

An investigation begins when something says an incident exists. The platform
does not poll and does not decide on its own.

| Trigger | Typical source |
|---|---|
| An alert fired | CloudWatch Alarms, Prometheus Alertmanager, Grafana Alerting, Datadog Monitors |
| A page was raised | PagerDuty, Opsgenie, incident.io |
| A human asked | The API, directly |
| A schedule | A periodic sweep, for slow-burning conditions |

**If an environment has no alerting, the platform has no starting point.** That
is worth checking before anything else — it is the single most common reason an
adoption stalls, and it is not something the platform can work around.

---

## What the platform needs to inspect

Eight capabilities. Each answers one question an incident responder actually
asks, and each maps to whatever tool holds that answer.

| Capability | The question it answers | Commonly implemented by | Status |
|---|---|---|---|
| `list_active_alerts` | What else is broken right now? | CloudWatch Alarms · Alertmanager · Datadog Monitors · Grafana | ✅ |
| `list_recent_deployments` | Did something change just before this? | ECS · Kubernetes · ArgoCD · GitHub Actions · Spinnaker | ✅ |
| `describe_compute_service` | Is the application actually running? | ECS · Kubernetes · EC2 ASG · Lambda · Nomad | ✅ |
| `describe_datastore` | Is the database healthy, or at a limit? | RDS · Aurora · self-hosted Postgres/MySQL · DynamoDB | ✅ |
| `describe_cache` | Is the cache evicting or saturated? | ElastiCache · self-hosted Redis · Memcached | ✅ |
| `describe_queue` | Is work piling up, and since when? | SQS · Kafka · RabbitMQ · Redis Streams · Pub/Sub | ✅ |
| `search_logs` | What did the application say while it broke? | Logs Insights · Loki · Elasticsearch · Splunk · Datadog | ❌ **missing** |
| `query_system_of_record` | What does the durable data say happened? | Read replica of the application database | ❌ **missing** |

The first six exist. The last two are the gaps, and both matter more than their
absence suggests.

### Why `search_logs` matters

Metrics tell you *that* something broke. Logs are usually the only thing that
says *what*. An agent that can read the alarm and the task count but not the
error text is guessing at the last and most important step.

The capability is a bounded query, not log streaming: a time range, a filter, a
result cap. Everything on the market supports that shape.

### Why `query_system_of_record` matters

This one is easy to miss, and reality keeps making the case for it.

Telemetry retention is short and uneven — often days, sometimes one. Incidents
get reported late: a customer dispute, a reconciliation break, a number that
looks wrong on a Monday. By the time anyone investigates, the logs are gone and
the metrics have been rolled up.

**The application database is frequently the only durable evidence left.** An
investigation platform that cannot read it is useless for exactly the class of
incident that most needs investigating — the one nobody noticed in time.

Constraints, because this one touches real data:

- **Read-only, against a replica.** Never the primary.
- **Parameterized queries only.** No string interpolation into SQL, ever. The
  arguments come from a model.
- **A statement timeout and a row cap**, enforced by the tool rather than
  requested politely.
- **Column-level exclusions** for anything the environment classifies as
  sensitive — the results end up in a prompt.

---

## Adapting an environment

Three steps, none of which touch the agents:

1. **Implement the capabilities you have.** Partial is fine. An environment with
   no queue simply has no `describe_queue`; the Infrastructure Agent works with
   what it is given and reports the gap rather than assuming a value.
2. **Declare the source in `aic.yaml`,** with its risk ceiling.
3. **Point a trigger at the API.**

What never changes: the agents, the prompts, the orchestration graph, the
guardrails, the API. If adapting an environment requires touching any of those,
the capability boundary is drawn in the wrong place and that is a bug in this
design, not in the environment.

---

## Checking an environment before adopting

`aic doctor` probes what is configured. This is the wider question — whether the
environment can support an investigation at all. It is worth answering honestly
up front, because the failure mode otherwise is adopting the platform and
concluding it is not useful, when the real finding is that nothing was
observable in the first place.

| Question | If the answer is no |
|---|---|
| Does anything alert? | There is no trigger. Nothing downstream matters until there is. |
| Do logs outlive a report cycle? | Late-reported incidents are uninvestigable, whatever tooling you add. |
| Do application metrics reach a queryable store? | The platform sees infrastructure health but never application behaviour. |
| Do critical events log at a level production keeps? | A signal filtered out is a signal that does not exist. |
| Is there a read-only path to the durable data? | The last resort for a cold investigation is closed. |

A "no" on the first two is not a reason to skip the platform — it is a finding
in its own right, and usually a cheaper thing to fix than anything else on the
list.
