# Testing LangGraph workflows

Test graph behavior at three levels.

## 1. Pure node tests

Call node functions with representative state and assert partial updates, validation, and error classification. Use deterministic fakes for model and tool boundaries; do not assert only that a mock was called.

## 2. Compiled graph path tests

Invoke a compiled graph with fixed input and assert the final state plus the path-sensitive outcomes. Cover:

- normal completion;
- every conditional branch;
- malformed or unavailable tool/model output;
- retry exhaustion and explicit failure termination;
- loop bounds and no-progress detection;
- fan-out/fan-in merge behavior.

Prefer an in-memory checkpointer for fast tests. Assert the state updates that matter to callers, not private implementation details.

## 3. Resume and integration tests

For persistence, interrupt after a known node, inspect the checkpoint, resume with the approved command, and verify the final state. Simulate a restart and ensure the same `thread_id` resumes. For external side effects, assert idempotency or use a test store that records duplicate attempts.

## Review checklist

- Is the state schema typed and serializable?
- Can every cycle prove progress and termination?
- Are retries limited and safe for side effects?
- Are route names and interrupt payloads stable?
- Does the test suite detect a wrong branch, lost state update, or duplicate side effect?
