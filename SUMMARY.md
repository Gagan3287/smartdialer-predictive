# SmartDialer Design Summary

## Question
**"How would you build a SmartDialer that gets as much utilization benefit of predictive dialing as possible while retaining the deterministic safety of progressive dialing?"**

---

## Technical Answer

To maximize agent utilization via predictive dialing while retaining the strict, zero-abandon safety of progressive dialing, the system must implement a **Hybrid Dual-Engine Architecture with Dynamic Circuit-Breaker Fallback**.

```mermaid
graph TD
    M[Telemetry & Metrics Pipeline] -->|Real-time Answer Rate & Agent Velocity| PE[Predictive Pacing Engine]
    PE -->|Calculates N_predictive| SC[Safety Controller]
    
    SC -->|Abandon Rate <= 3.0% & High Confidence| CA[Predictive Call Allocator]
    SC -->|Abandon Rate > 3.0% OR Agent Shortage| PD[Standalone Progressive Dialer]

    CA -->|Over-dial N:1| Telecom[Telecom Provider]
    PD -->|Strict 1:1 Dialing| Telecom
```

### 1. Dual Pacing Modes with Deterministic Circuit Breaker
- **Normal Operating Envelope (Predictive Mode)**: The Predictive Pacing Engine computes expected agent free rates based on rolling setup times ($T_{setup}$), talk times ($T_{talk}$), and windowed answer rates ($P_{ans}$). It over-dials calls to fill anticipated agent availability.
- **Safety Gatekeeper (Safety Controller)**: Sits downstream of the pacing engine. If the rolling abandon rate breaches compliance limits ($>3.0\%$) or available agent buffer collapses below safety thresholds, the controller immediately forces a deterministic fallback to the **Standalone Progressive Dialer**.

### 2. Standalone Progressive Guardrail
- The Progressive Dialer operates on strict $1:1$ logic ($1 \text{ available agent} \rightarrow 1 \text{ call attempt}$).
- **Pre-Dial Agent Locks**: Marks agents `RESERVED` before initiating telephony requests.
- **Pre-Dispatch Verification**: Re-verifies agent state in database millisecond prior to call setup. If an agent pauses, logs off, or disappears mid-setup, the attempt is aborted instantly, preventing orphaned ringing calls.
- **Automatic Failure Recovery**: If a progressive call fails or times out, the agent is released back to `AVAILABLE`, and the borrower retry policy is enforced.

### 3. Dynamic Buffer Allocation & Quantile Confidence Windows
- Rather than static multipliers, the predictive engine scales over-dialing using confidence intervals. When historical answer rate variance is low, over-dialing is expanded to maximize utilization. When variance spikes or call setup time increases, the safety controller shrinks the over-dialing window toward $1:1$ progressive allocation.

By isolating the **Predictive Pacing Engine** from telecom execution and routing all dialing decisions through the **Safety Controller** and **Progressive Dialer**, the system delivers maximal utilization during stable conditions and deterministic, zero-abandon compliance during sudden traffic spikes or provider anomalies.
