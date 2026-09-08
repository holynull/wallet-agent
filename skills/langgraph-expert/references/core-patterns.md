# Core graph patterns

Use this as a design checklist; confirm exact signatures against the installed version.

## State

Model state as the smallest durable contract. Include only values needed by downstream nodes, and decide whether each field is replaced or reduced when multiple updates arrive. In Python, a `TypedDict` is a common default; in TypeScript, use an explicit state annotation or schema supported by the installed release.

Document for each field:

| Field | Producer | Consumers | Merge rule | Serializable? |
|---|---|---|---|---|

Avoid storing live clients, callbacks, open files, or model objects in checkpointed state. Store identifiers and reconstruct resources inside nodes.

## Nodes

A node should do one coherent transition: read state, perform bounded work, and return a partial state update. Keep provider calls and side effects behind small functions so the transition can be tested without a live service.

## Edges and termination

Use ordinary edges for unconditional sequencing and conditional edges for policy. Name route outcomes (`"answer"`, `"tool"`, `"retry"`) rather than returning opaque booleans. For every cycle, define a progress measure and a hard stop (attempt count, budget, or explicit terminal decision). Test that the stop is reachable.

## Parallel work

Use fan-out only when branches are independent and their updates have a defined reducer/merge rule. Make ordering irrelevant or explicit; never rely on incidental completion order. Fan-in should validate that all required branch results exist before continuing.

## Minimal shape

Prefer a tiny graph with one state schema, two or three nodes, an explicit conditional route, and a terminal node before adding agent abstractions. This makes routing and state evolution observable.
