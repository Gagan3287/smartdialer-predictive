# SmartDialer Predictive Mode Prototype

A production-grade, single-process prototype of a Predictive SmartDialer designed for outbound collections call centers. Built with **Python (FastAPI)**, **SQLite (WAL Mode)**, and **`asyncio` concurrency controls**.

---

## Key Features & Architectural Layering

The system strictly enforces 5 pipeline layers:
$$\text{Campaign} \longrightarrow \text{Predictive Pacing Engine} \longrightarrow \text{Safety Controller} \longrightarrow \text{Call Allocator / Progressive Dialer} \longrightarrow \text{Telecom Provider Interface}$$

1. **Explicit State Machines**:
   - **Agent State Machine**: States (`OFFLINE`, `AVAILABLE`, `RESERVED`, `DIALING`, `CONNECTED`, `WRAP_UP`, `PAUSED`). Enforces concurrency safety using per-agent `asyncio.Lock` and optimistic locking (`version` column).
   - **Call State Machine**: Idempotent against duplicate events, out-of-order events, and worker task crashes.
2. **Predictive Pacing Engine**: Rule-based pacing calculation computing expected agent free rates, ringing answer probabilities, and raw call recommendations $N$ with full step-by-step reasoning logs.
3. **Safety Controller**: Independent compliance gatekeeper capping calls, monitoring abandon rate limits ($3.0\%$), and triggering fallback to Progressive mode.
4. **Standalone Progressive Dialer**: Dedicated $1:1$ allocation module reserving agents prior to dialing, handling agent dropouts, failure retries, and preventing over-allocation.
5. **Telecom Provider Drivers**: Swappable interfaces for `ProviderA` (fast & reliable) and `ProviderB` (slow, flaky, duplicate/out-of-order events).
6. **Worker Crash Recovery**: Background reconciliation engine detecting half-committed calls/agents and releasing orphaned resources.
7. **Failure Simulations & Load Testing**: Automated simulation suite and load testing framework measuring latency and throughput for 100, 500, and 1000 agents.

---

## Installation & Setup

### Prerequisites
- Python 3.10+
- `pip`

### Step 1: Install Dependencies
```bash
pip install -r requirements.txt
```
*(Or install directly: `pip install fastapi uvicorn pydantic pytest-asyncio aiosqlite`)*

---

## Running Tests & Simulations

### 1. Run Unit Tests (State Machines, Import Boundaries, Pacing, Progressive Dialer)
```bash
python -m pytest tests/
```

### 2. Run Individual Failure Simulations
```bash
# Worker task crash mid-call (RESERVED -> task.cancel() -> Recovery reconciliation)
python -m simulations.sim_worker_crash

# Telecom Provider Outage (100% failure -> abandon rate spike -> Safety FALLBACK_PROGRESSIVE)
python -m simulations.sim_provider_outage

# Sudden Agent Drop (100 -> 60 agents -> reaction latency & pacing reduction)
python -m simulations.sim_agent_drop

# Duplicate Event Flood (Idempotency test)
python -m simulations.sim_duplicate_flood

# Out-of-Order Event Flood (COMPLETED before ANSWERED)
python -m simulations.sim_out_of_order_flood
```

### 3. Run Full Scenario Suite (Scenarios A/B/C/D) & Generate HTML Report
```bash
python -m simulations.scenario_runner
```
*HTML report generated at `reports/scenario_report.html`.*

### 4. Run Concurrency Load Testing (100, 500, 1000 Agents)
```bash
python -m simulations.load_test
```

---

## Running the Web API & Dashboard

Start the FastAPI application:
```bash
uvicorn app.main:app --reload --port 8000
```
- **Interactive Swagger Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Scenario Benchmark Report**: [http://localhost:8000/report](http://localhost:8000/report)

---

## Project Structure

```
├── app/
│   ├── main.py                  # FastAPI Application endpoints
│   ├── db/                      # SQLite WAL database & tables
│   ├── state_machines/          # Agent & Call state machine logic & locks
│   ├── providers/               # Telecom Provider A & B implementations
│   ├── pacing/                  # Predictive Pacing Engine
│   ├── safety/                  # Safety Controller
│   ├── progressive/             # Standalone Progressive Dialer module
│   ├── allocator/               # Call Allocator
│   ├── campaign/                # Campaign Manager pipeline coordinator
│   └── reconciliation/          # Worker crash recovery engine
├── tests/                       # Pytest test suite
├── simulations/                 # Failure simulations & scenario runner
├── reports/                     # Generated HTML scenario benchmark reports
├── README.md                    # Setup & user documentation
├── SUMMARY.md                   # Predictive vs Progressive design rationale
├── ARCHITECTURE.md              # Pipeline architecture & state diagrams
└── ADR.md                       # Architectural Decision Record & 1k->10k scale path
```

---

## Core Design Summary

> **"How would you build a SmartDialer that gets as much utilization benefit of predictive dialing as possible while retaining the deterministic safety of progressive dialing?"**

The solution combines a **Predictive Pacing Engine** for high-confidence over-dialing during stable periods with an independent **Safety Controller** and **Standalone Progressive Dialer**. When historical abandon rates breach $3.0\%$ or available agent buffers collapse, the Safety Controller immediately diverts call allocation to the Progressive Dialer. The Progressive Dialer locks agents prior to dialing, verifies agent availability millisecond before call dispatch, and enforces strict $1:1$ allocation with automatic failure cleanup. See [`SUMMARY.md`](file:///e:/internship/SUMMARY.md) for full technical details.
