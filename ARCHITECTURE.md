# SmartDialer System Architecture

SmartDialer is an outbound collections dialing system built with Python, FastAPI, SQLite, and `asyncio`. It is designed with strict pipeline layering to guarantee concurrency safety, operational compliance, and decision explainability.

## High-Level Architecture Pipeline

```mermaid
graph TD
    subgraph Campaign Management
        CM[Campaign Manager]
    end

    subgraph Decision Engine
        PE[Predictive Pacing Engine]
        SC[Safety Controller]
    end

    subgraph Execution & Allocation
        CA[Predictive Call Allocator]
        PD[Standalone Progressive Dialer]
    end

    subgraph Telecom & Data Layer
        TPI[Telecom Provider Interface]
        DB[(SQLite WAL Persistence)]
    end

    CM -->|1. Trigger Dial Loop| PE
    PE -->|2. Recommended N + Rationale| SC
    SC -->|3a. Abandon rate OK and provider healthy| CA
    SC -->|3b. Abandon rate > 3% OR failure rate > threshold| PD
    CA -->|4a. Dial without pre-reserving agent| TPI
    PD -->|4b. Reserve agent first then Dial 1:1| TPI
    TPI -->|5. Telephony Events| DB
```

**Key pipeline invariants:**
- The Predictive Call Allocator dials calls **without pre-reserving an agent**. Agent reservation is attempted only after a borrower answers. If no agent is free at that moment, the call transitions to `ABANDONED` — a true compliance-relevant terminal state, distinct from `FAILED` (no answer / provider error).
- The Standalone Progressive Dialer retains the original guarantee: it marks an agent `RESERVED` *before* initiating the telephony request, enforcing strict 1:1 allocation.
- The Safety Controller evaluates **two independent signals**: `rolling_abandon_rate` (borrower-side risk) and `rolling_failure_rate` (provider/technical health). Either alone can trigger `FALLBACK_PROGRESSIVE`.

---

## Core Components

| Component | Module | Responsibility |
|---|---|---|
| **Campaign Manager** | `app/campaign/` | Drives the per-campaign execution loop; triggers pacing evaluation and dispatch each tick. |
| **Predictive Pacing Engine** | `app/pacing/` | Computes optimal call-launch volume *N* from rolling answer rate, setup times, and agent state velocity. Zero dependencies on Telecom or Allocator. |
| **Safety Controller** | `app/safety/` | Independent compliance gatekeeper. Evaluates abandon rate and provider failure rate; caps or redirects calls to Progressive mode when thresholds are breached. |
| **Predictive Call Allocator** | `app/allocator/` | Executes approved predictive batches. Dials without pre-reserving an agent; reserves an agent only after a borrower answers. |
| **Standalone Progressive Dialer** | `app/progressive/` | Deterministic 1:1 allocator. Reserves the agent before dialing; handles agent dropouts, failure retries, and concurrency limits. |
| **Telecom Provider Interface** | `app/providers/` | Abstract driver enabling pluggable providers (`ProviderA` fast/reliable vs `ProviderB` slow/flaky with duplicate/out-of-order events). |
| **Reconciliation Engine** | `app/reconciliation/` | Background task that detects half-committed calls/agents (e.g. after a worker crash) and releases orphaned resources. |

---

## State Diagrams

### Call State Machine

States are defined in `app/state_machines/call_state.py`.

**Terminal states**: `COMPLETED`, `FAILED`, `CANCELLED`, `ABANDONED`

```mermaid
stateDiagram-v2
    [*] --> QUEUED

    QUEUED --> RESERVED : Progressive - agent pre-reserved
    QUEUED --> INITIATED : Predictive - dial without pre-reserving
    QUEUED --> CANCELLED : Campaign stopped

    RESERVED --> INITIATED : Dispatched to telecom
    RESERVED --> FAILED : Setup error
    RESERVED --> CANCELLED : Agent dropped before dispatch

    INITIATED --> RINGING : Telephony setup complete
    INITIATED --> FAILED : Network / provider error
    INITIATED --> CANCELLED : Aborted before ringing
    INITIATED --> COMPLETED : Immediate provider acknowledgement

    RINGING --> ANSWERED : Borrower picked up
    RINGING --> FAILED : Timeout / busy / no answer
    RINGING --> CANCELLED : Aborted while ringing
    RINGING --> COMPLETED : Fast hangup at ringing stage
    RINGING --> ABANDONED : Borrower answered no agent available

    ANSWERED --> CONNECTED : Bridged to available agent
    ANSWERED --> FAILED : Bridge failure
    ANSWERED --> COMPLETED : Dropped before bridge
    ANSWERED --> ABANDONED : No agent free at answer time

    CONNECTED --> COMPLETED : Call ended normally
    CONNECTED --> FAILED : Mid-call drop

    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
    ABANDONED --> [*]
```

**Note on `ABANDONED`**: This is a compliance-relevant terminal state meaning the borrower answered but no agent was available to take the call. It is **distinct from `FAILED`** (which means the borrower never answered, or a technical failure occurred). Only predictive calls can reach `ABANDONED`; progressive calls never can, because agents are pre-reserved before dialing.

---

### Agent State Machine

States are defined in `app/state_machines/agent_state.py`.

```mermaid
stateDiagram-v2
    [*] --> OFFLINE

    OFFLINE --> AVAILABLE : Log in / Ready
    OFFLINE --> PAUSED : Break before going live

    AVAILABLE --> RESERVED : Reserved for call
    AVAILABLE --> PAUSED : Agent takes a break
    AVAILABLE --> OFFLINE : Agent logs out

    RESERVED --> DIALING : Provider dialing initiated
    RESERVED --> AVAILABLE : Call aborted / failed before dial
    RESERVED --> PAUSED : Agent pauses mid-reservation
    RESERVED --> OFFLINE : Agent logs out mid-reservation

    DIALING --> CONNECTED : Borrower answered agent bridged
    DIALING --> WRAP_UP : No answer / answering machine
    DIALING --> AVAILABLE : Provider failure quick release
    DIALING --> PAUSED : Agent pauses during dial
    DIALING --> OFFLINE : Agent logs out during dial

    CONNECTED --> WRAP_UP : Call ended after-call work begins
    CONNECTED --> AVAILABLE : Quick release no wrap-up needed

    WRAP_UP --> AVAILABLE : After-call work complete
    WRAP_UP --> PAUSED : Agent pauses during wrap-up
    WRAP_UP --> OFFLINE : Agent logs out during wrap-up

    PAUSED --> AVAILABLE : Unpause / return from break
    PAUSED --> OFFLINE : Log out while paused
```

---

## Concurrency Safety

All state transitions are protected by two independent guards:

1. **Per-entity `asyncio.Lock`** (`AgentLockRegistry`, `CallLockRegistry`): Enforces strict mutual exclusion in memory before any SQLite write, preventing race conditions across concurrent async tasks within the single process.
2. **Optimistic Locking (`version` column)**: Every `UPDATE` requires `WHERE id = ? AND version = ?`. A `rowcount != 1` result means a concurrent writer won the race; the failing task raises `OptimisticLockError` rather than silently applying a stale update.

This dual-guard pattern makes state machine transitions atomic and idempotent against duplicate events, out-of-order events, and worker task crashes.

---

## Persistence

SQLite in **Write-Ahead Logging (WAL)** mode provides:
- Concurrent async reads alongside a single writer (suitable for single-process prototype workloads)
- Full ACID transactions with zero infrastructure overhead
- Simple deployment and fully reproducible failure simulations

See [ADR.md](ADR.md) for the detailed technology stack rationale and the scaling path to PostgreSQL + Redis at 1,000-10,000 agents.
