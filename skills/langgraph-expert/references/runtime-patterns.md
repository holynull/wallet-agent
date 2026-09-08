# Runtime patterns

Confirm exact imports and configuration keys against the installed LangGraph release and its persistence backend.

## Persistence and resume

- Compile with an appropriate checkpointer for the deployment (in-memory for local tests, durable storage in production).
- Invoke with a stable `thread_id` scoped to the conversation or job, not a random ID per request.
- Keep checkpointed state serializable and versionable. Plan migrations when state fields change.
- Test a process interruption between nodes, then resume and assert no duplicate irreversible side effect.

## Human-in-the-loop

Use an interrupt when execution must pause for an external decision. Define the payload shown to the reviewer and the command/resume value accepted afterward. Treat the interrupted node as replayable: place irreversible work after approval or guard it with an idempotency key.

## Commands and routing

Use a command-style update only when a node must update state and choose the next destination together. Keep route names stable because persisted executions and observability tools may refer to them.

## Streaming and observability

Choose a stream mode that matches the consumer: state updates for UI, messages/tokens for chat, or event/debug data for tracing. Avoid logging secrets or full prompts by default. Include a run/thread identifier in structured logs and capture node name, duration, outcome, and error class.

## Retries and failures

Retry only transient, bounded failures. Do not retry validation errors or non-idempotent side effects without a key. Represent exhaustion explicitly in state so the graph reaches a tested failure terminal instead of looping forever.
