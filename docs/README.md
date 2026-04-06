# Documentation

This folder contains documentation for the distributed system project, explaining the structure, architecture, and design decisions. Diagrams are provided to visualize the system and event flows.

## Available Documents

- [Checkpoint #1 Documentation](./checkpoint-1.md) — System overview, architecture, and gRPC design
- [Checkpoint #2 Documentation](./checkpoint-2.md) — Vector clocks, queue, executors, leader election
- [Event Ordering Design](./event_ordering_design.md) — Seminar 5: vector clock rules and 6-event partial order

## Diagrams

- `images/architecture.png` — Service topology and gRPC communication
- `images/systemdiagram.png` — High-level execution flow (user → frontend → orchestrator → services)
- `images/eventorderdiagram.png` — Event order DAG showing causal dependencies
- `images/vectorclocks.png` — Vector clock values at each event in a sample order execution
