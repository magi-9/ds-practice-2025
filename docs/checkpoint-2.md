# Checkpoint #2 Documentation

This document summarizes the implementation for Checkpoint #2 (Seminars 5 & 7): event ordering with vector clocks, and leader election with a queue and executor services.

---

## 1) System Overview

The system extends Checkpoint #1 with two new concerns:

**Seminar 5 — Event Ordering (Vector Clocks)**

The three backend services (`fraud_detection`, `transaction_verification`, `suggestions`) now execute events in a defined causal order, tracked via vector clocks. Each service initializes a vector clock per order, increments it on each event, and propagates it in every gRPC message.

Full event order design and vector clock rules: [event_ordering_design.md](./event_ordering_design.md)

**Seminar 7 — Leader Election & Mutual Exclusion**

Two new services are added:

- `order_queue` (gRPC, port `50054`): FIFO queue for validated orders. Also acts as the coordinator for leader election via `RegisterExecutor`.
- `order_executor` (gRPC, replicated ×2): polls the queue and executes orders. Only the elected leader may dequeue.

## 2) Updated Architecture

The services from Checkpoint #1 are unchanged. New additions:

- Orchestrator now calls `Enqueue` on `order_queue` after successful verification, and waits for confirmation before replying to the user.
- Two `order_executor` replicas start up and register with `order_queue`. The first to register becomes the leader and is the only one allowed to `Dequeue`.

![Architecture](/docs/images/architecture.png)

## 3) Event Ordering & Vector Clocks

See [event_ordering_design.md](./event_ordering_design.md) for the full partial order, vector clock rules, and logging format.

**6 events implemented (a–f):**

| Event | Service                  | Description                    |
| ----- | ------------------------ | ------------------------------ |
| a     | transaction_verification | Verify items list is not empty |
| b     | transaction_verification | Verify mandatory user data     |
| c     | transaction_verification | Verify credit card format      |
| d     | fraud_detection          | Check user data for fraud      |
| e     | fraud_detection          | Check credit card for fraud    |
| f     | suggestions              | Generate book suggestions      |

**Partial order:** `a ∥ b`, `c → a`, `d → b`, `e → {c,d}`, `f → e`

**Event order diagram:**

![Event order diagram](/docs/images/eventorderdiagram.png)

**Vector clocks diagram:**

![Vector clocks diagram](/docs/images/vectorclocks.png)

## 4) Leader Election

**Algorithm:** Centralized — the `order_queue` service acts as coordinator.

- Each `order_executor` replica calls `RegisterExecutor(executor_id)` on startup.
- The **first** replica to register receives a `leader_token` and becomes the leader.
- All subsequent replicas receive an empty token and remain passive.
- Only the replica holding the valid `leader_token` can call `Dequeue`. All other attempts are rejected with `"Not leader"`.
- Mutual exclusion is enforced inside the queue via `RLock` — concurrent dequeue requests are serialized.

**Why centralized?** Simple, deterministic, no split-brain risk. The queue is already a single point of truth for the order state, so co-locating the election there adds no new dependency.

**Leader election sequence:**

![Leader election sequence](/docs/images/leader_election.png)

## 5) Failure Modes

| Component                                      | Failure          | Effect                                                                 | Handling                                                                                                   |
| ---------------------------------------------- | ---------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `fraud_detection` / `transaction_verification` | Crash or timeout | Order rejected, `503` returned to user                                 | gRPC timeout (3s); orchestrator returns error                                                              |
| `suggestions`                                  | Crash or timeout | Order still approved, empty suggestions list                           | Graceful degradation — not a critical path                                                                 |
| `order_queue`                                  | Crash            | No new orders can be enqueued or dequeued; executor loses leader state | Orchestrator returns `503`; executors retry registration on restart                                        |
| `order_executor` leader                        | Crash            | No orders dequeued until queue restarts                                | Queue holds stale `leader_id`; follower cannot self-promote (known limitation)                             |
| `order_executor` follower                      | Crash            | No effect — follower is passive                                        | Leader continues unaffected                                                                                |
| `orchestrator`                                 | Crash mid-flow   | Order data may remain cached in backend services                       | `ClearOrder` is called on both success and failure paths, but not if orchestrator crashes before broadcast |

## 6) Bonus — Broadcast ClearOrder

After every order (success or failure), the orchestrator broadcasts `ClearOrder` to all three backend services, attaching the final vector clock `VCf`. Each service verifies its local `VC ≤ VCf` before clearing the order data. If the local VC is causally ahead of `VCf`, the service refuses and logs an error.
