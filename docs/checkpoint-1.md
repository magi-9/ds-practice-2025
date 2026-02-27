# Checkpoint #1 Documentation

This document summarizes the current state of the distributed system for Seminar 4 Checkpoint #1.

## 1) System Overview

The solution consists of one frontend, one REST orchestrator, and three gRPC backend services:

- `frontend` (Nginx static page): user checkout form and response display.
- `orchestrator` (Flask REST): validates request, calls backend services concurrently, merges results.
- `fraud_detection` (gRPC): evaluates fraud risk rules.
- `transaction_verification` (gRPC): validates transaction and payment fields.
- `suggestions` (gRPC): returns recommended books.

## 2) Architecture Diagram

![Architecture diagram](/docs/images/architecture.png)

- **Description**:
  - Frontend serves the UI on `http://localhost:8080`.
  - Orchestrator exposes a REST endpoint `POST /checkout` on `http://localhost:8081`.
  - Orchestrator calls three internal gRPC services inside the Docker Compose network:
    - fraud_detection (gRPC, port `50051`)
    - transaction_verification (gRPC, port `50052`)
    - suggestions (gRPC, port `50053`)
  - Services communicate using Docker Compose service names (e.g., `fraud_detection:50051`).

## 3) System Diagram (Execution Flow)

![System diagram](/docs/images/systemdiagram.png)

- **Description**:
  - User clicks "Submit Order" button
  - Frontend sends POST /checkout to Orchestrator
  - Orchestrator receives request and spawns 3 worker threads
  - Threads call fraud detection, transaction verification, and suggestions in parallel
  - Orchestrator waits for all results
  - Decision: reject if fraud or invalid else approve and sugest
  - Orchestrator sends REST response back to frontend

## 4) Design Decisions

- **Orchestrator as single REST entrypoint**: keeps frontend simple and hides backend topology.
- **gRPC for internal services**: typed contracts and clear service boundaries.
- **Parallel backend calls (threading)**: lower response latency than sequential calls.
- **Graceful degradation**: suggestions failures do not block checkout, while critical verification/fraud failures return service-unavailable.
- **Structured logs**: each service logs key request handling and outcomes for demo/debug visibility.
