import asyncio
import os
import time
import logging
from typing import Dict, Any, List
import aiosqlite

from app.db.database import get_db_connection, init_db
from app.state_machines.agent_state import AgentState, AgentStateMachine, AgentLockRegistry
from app.state_machines.call_state import CallLockRegistry
from app.providers.provider_a import ProviderA

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_test")

async def run_reservation_benchmark(agent_count: int, concurrency_level: int) -> Dict[str, Any]:
    db_path = f"load_test_{agent_count}.db"
    os.environ["SMARTDIALER_DB_PATH"] = db_path
    AgentLockRegistry.reset()
    CallLockRegistry.reset()
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except PermissionError:
            pass

    import app.db.database as db_mod
    db_mod.DB_PATH = db_path
    await init_db()

    conn = await get_db_connection()
    now = time.time()

    # Seed agents and borrowers
    for i in range(1, agent_count + 1):
        await conn.execute(
            "INSERT INTO agents (id, name, state, version, updated_at) VALUES (?, ?, ?, 1, ?)",
            (f"agent-{i}", f"Agent {i}", AgentState.AVAILABLE.value, now)
        )
    for i in range(1, concurrency_level + 1):
        await conn.execute(
            "INSERT INTO borrowers (id, name, phone_number, attempts, status, updated_at) VALUES (?, ?, ?, 0, 'PENDING', ?)",
            (f"borr-{i}", f"Borrower {i}", f"+1555{i:05d}", now)
        )
    await conn.commit()
    await conn.close()

    successes = []
    failures = []
    latencies_ms = []

    async def worker_reservation_attempt(task_id: int):
        target_agent_id = f"agent-{(task_id % agent_count) + 1}"
        call_id = f"call-load-{task_id}"
        conn_local = await get_db_connection()
        t0 = time.time()
        try:
            res = await AgentStateMachine.reserve_agent(conn_local, target_agent_id, call_id=call_id)
            elapsed_ms = (time.time() - t0) * 1000.0
            successes.append(task_id)
            latencies_ms.append(elapsed_ms)
        except Exception as e:
            elapsed_ms = (time.time() - t0) * 1000.0
            failures.append((task_id, str(e)))
            latencies_ms.append(elapsed_ms)
        finally:
            await conn_local.close()

    start_time = time.time()
    tasks = [worker_reservation_attempt(i) for i in range(concurrency_level)]
    await asyncio.gather(*tasks)
    total_time = time.time() - start_time

    avg_latency = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0
    p95_latency = sorted(latencies_ms)[int(len(latencies_ms) * 0.95)] if latencies_ms else 0.0
    throughput_ops_sec = concurrency_level / total_time if total_time > 0 else 0.0

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except PermissionError:
            pass

    return {
        "agents": agent_count,
        "concurrency": concurrency_level,
        "total_time_sec": round(total_time, 3),
        "throughput_ops_sec": round(throughput_ops_sec, 2),
        "avg_latency_ms": round(avg_latency, 2),
        "p95_latency_ms": round(p95_latency, 2),
        "success_count": len(successes),
        "failure_count": len(failures)
    }

async def main():
    logger.info("Starting SmartDialer Concurrency Load Testing...")
    logger.info("-------------------------------------------------")
    
    benchmarks = [
        (100, 100),
        (500, 500),
        (1000, 1000)
    ]

    results = []
    for agents, concurrency in benchmarks:
        logger.info(f"Running Load Test: {agents} Agents under {concurrency} Concurrent Workers...")
        res = await run_reservation_benchmark(agents, concurrency)
        results.append(res)
        logger.info(
            f"Result: {agents} Agents | Ops/sec: {res['throughput_ops_sec']} | "
            f"Avg Latency: {res['avg_latency_ms']} ms | P95 Latency: {res['p95_latency_ms']} ms | "
            f"Success: {res['success_count']} | Failures: {res['failure_count']}"
        )

    logger.info("-------------------------------------------------")
    logger.info("LOAD TEST SUMMARY REPORT:")
    for r in results:
        print(
            f"Agents: {r['agents']:4d} | Concurrency: {r['concurrency']:4d} | "
            f"Throughput: {r['throughput_ops_sec']:7.2f} ops/s | "
            f"Avg Latency: {r['avg_latency_ms']:6.2f} ms | P95 Latency: {r['p95_latency_ms']:6.2f} ms"
        )

if __name__ == "__main__":
    asyncio.run(main())
