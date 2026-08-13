# Elevated 5xx Triage

The generic first-response procedure for a service returning server errors, used
when the failure mode is not yet known.

## Symptoms

- `HTTPCode_Target_5XX_Count` above its alarm threshold.
- Elevated `TargetResponseTime`, or a drop in `HealthyHostCount`.
- User reports of failures on a specific flow.

## Diagnosis

- Establish blast radius first: list every alarm currently firing. One service
  failing and one service plus its dependencies failing are different incidents.
- Establish the timeline: when did the error rate step up, and what else happened
  in that window — a deployment, a config change, a traffic shift, a dependency's
  own incident.
- Separate "the application is erroring" from "the platform cannot keep the
  application running". Task-level restarts and health-check failures indicate
  the latter.
- Read the actual error text before theorising. Connection-acquisition timeouts,
  downstream timeouts, and unhandled exceptions have different responses and look
  identical on a dashboard.

## Mitigation

- If a deployment correlates, roll it back and confirm recovery before
  investigating further.
- If a dependency is the source, apply the relevant dependency runbook rather
  than restarting this service.
- Restarting a service is a valid last resort to clear a stuck process, but it
  destroys the evidence needed to find the cause. Capture logs and metrics first.

## Escalation

- Escalate to the incident commander if the error rate does not fall within
  fifteen minutes of the first mitigation, or if blast radius is widening.
