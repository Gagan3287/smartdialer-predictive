# SmartDialer System Architecture

SmartDialer is an outbound collections dialing system built with Python, FastAPI, SQLite, and `asyncio`. It is designed with strict pipeline layering to guarantee concurrency safety, operational compliance, and decision explainability.

## High-Level Architecture Pipeline

```mermaid
graph TD
    subgraph Campaign Management
        CP[Campaign Engine]
    end

    subgraph Decision Engine
        PE[Predictive Pacing Engine]
        SC[Safety Controller]
    end

    subgraph Execution & Allocation
        PD[Progressive Dialer]
        CA[Call Allocator]
    end

    subgraph Telecom & Data Layer
        TPI[Telecom Provider Interface]
        DB[(SQLite WAL Persistence)]
    end

    CP -->|1. Trigger Dial Loop| PE
    PE -->|2. Recommendation N + Rationale| SC
    SC -->|3a. Approved/Reduced N| CA
    SC -->|3b. FALLBACK_PROGRESSIVE| PD
    PD -->|Deterministic 1:1 Dial| CA
    CA -->|4. Reserve Agent & Dial| TPI
    TPI -->|5. Telephony Events| DB
```

## Core Components

1. **Campaign Engine (`app/campaign/`)**: Coordinates campaign execution loop, triggering pacing evaluation and dispatch.
2. **Predictive Pacing Engine (`app/pacing/`)**: Computes optimal call launch volume $N$ based on rolling answer rate, setup times, and agent state velocity. Zero dependencies on Telecom or Allocator.
3. **Safety Controller (`app/safety/`)**: Independent compliance layer capping call rates based on hard limits and historical abandon rate. Triggers Progressive Fallback when safety thresholds are breached.
4. **Progressive Dialer (`app/progressive/`)**: Dedicated module providing deterministic 1:1 agent-to-borrower call allocation, handling agent dropouts, failure retries, and concurrency limits.
5. **Call Allocator (`app/allocator/`)**: Executes approved predictive call batches by reserving available agents and interfacing with Telecom Providers.
6. **Telecom Provider Interface (`app/providers/`)**: Abstract driver enabling pluggable providers (`ProviderA` fast/reliable vs `ProviderB` slow/flaky).

## State Diagrams

### Agent State Machine

```mermaid
stateDiagram-v2
    [*] --> OFFLINE
    OFFLINE --> AVAILABLE: Log in / Ready
    OFFLINE --> PAUSED: Break
    PAUSED --> AVAILABLE: Unpause
    PAUSED --> OFFLINE: Log out
    AVAILABLE --> RESERVED: Reserved for Call
    AVAILABLE --> PAUSED: Break
    AVAILABLE --> OFFLINE: Log out
    RESERVED --> DIALING: Provider Dialing
    RESERVED --> AVAILABLE: Call Aborted / Failed Setup
    DIALING --> CONNECTED: Customer Answered
    DIALING --> WRAP_UP: No Answer / Answering Machine
    DIALING --> AVAILABLE: Provider Failure
    CONNECTED --> WRAP_UP: Call Ended
    CONNECTED --> AVAILABLE: Quick Release
    WRAP_UP --> AVAILABLE: After-call Work Complete
```

### Call State Machine

```mermaid
stateDiagram-v2
    [*] --> QUEUED
    QUEUED --> RESERVED: Allocated to Agent
    QUEUED --> CANCELLED: Campaign Stopped
    RESERVED --> INITIATED: Dispatched to Telecom
    RESERVED --> FAILED: Setup Error
    RESERVED --> CANCELLED: Agent Dropped
    INITIATED --> RINGING: Telephony Setup
    INITIATED --> FAILED: Network Failure
    RINGING --> ANSWERED: Customer Picked Up
    RINGING --> COMPLETED: Fast Hangup
    RINGING --> FAILED: Timeout / Busy
    ANSWERED --> CONNECTED: Bridged to Agent
    ANSWERED --> COMPLETED: Dropped before Bridge
    CONNECTED --> COMPLETED: Call Ended
    CONNECTED --> FAILED: Mid-call Drop
```
