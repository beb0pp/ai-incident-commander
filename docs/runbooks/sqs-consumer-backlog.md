# SQS Consumer Backlog

Applies when a queue's depth grows without bound and the age of the oldest
message climbs, indicating consumers cannot keep up or have stopped.

## Symptoms

- `ApproximateNumberOfMessages` rises steadily.
- `ApproximateAgeOfOldestMessage` climbs past the queue's normal ceiling.
- `ApproximateNumberOfMessagesNotVisible` is near zero (nothing is being
  processed) or pinned at the consumer concurrency limit (processing is slow).
- The dead-letter queue may be filling if `maxReceiveCount` is being reached.

## Diagnosis

- Read the queue attributes and note which of the two shapes above applies:
  stalled consumers versus slow consumers.
- Check whether the consumer service is healthy — a backlog is frequently a
  symptom of a consumer that is crash-looping or blocked on a dependency.
- Sample the dead-letter queue. Repeated identical failures point at a poison
  message; varied failures point at a downstream outage.
- Check whether visibility timeout is shorter than actual processing time, which
  causes the same message to be redelivered and processed repeatedly.

## Mitigation

- If consumers are unhealthy, fix the consumer. Draining a queue whose consumer
  is broken accomplishes nothing.
- If consumers are healthy but saturated, scale consumer concurrency.
- Raise the visibility timeout if redelivery is the driver.
- Redrive the dead-letter queue only after the underlying failure is fixed.
- Do not purge the queue. Purging discards unprocessed business events and is
  almost never recoverable.

## Rollback

- Scaling consumers back down is safe once oldest-message age returns to normal.

## Escalation

- Escalate to the owning team if the backlog persists after the consumer is
  healthy and scaled, which indicates a throughput ceiling downstream.
