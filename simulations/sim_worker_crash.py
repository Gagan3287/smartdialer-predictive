import asyncio
import os
import time
import logging
from app.db.database import get_db_connection, init_db, DB_PATH
from app.state_machines.agent_state import AgentState, AgentStateMachine, AgentLockRegistry
from app.state_machines.call_state import CallState, CallStateMachine, CallLockRegistry
from app.reconciliation.recovery import WorkerCrashRecoveryEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sim_worker_crash")

SIM_DB_PATH = "sim_worker_crash.db"

async def run_simulation():
    # Setup DB
    os.environ["SMARTDIALER_DB_PATH"] = SIM_DB_PATH
    AgentLockRegistry.reset()
    CallLockRegistry.reset()
    if os.path.exists(SIM_DB_PATH):
        try:
            os.remove(SIM_DB_PATH)
        except PermissionError:
            pass

    import app.db.database as db_mod
    db_mod.DB_PATH = SIM_DB_PATH
    await init_db()

    conn = await get_db_connection()
    now = time.time()

    # Seed Agent & Borrower
    await conn.execute(
        "INSERT INTO agents (id, name, state, version, updated_at) VALUES (?, ?, ?, 1, ?)",
        ("agent-crash-1", "Crash Test Agent", AgentState.AVAILABLE.value, now)
    )
    await conn.execute(
        "INSERT INTO borrowers (id, name, phone_number, attempts, status, updated_at) VALUES (?, ?, ?, 0, 'PENDING', ?)",
        ("borr-crash-1", "Crash Borrower", "+155599900", now)
    )
    await conn.commit()

    reserved_committed_event = asyncio.Event()
    call_id = "call-crash-101"

    async def worker_dial_task():
        w_conn = await get_db_connection()
        try:
            # 1. Create QUEUED Call
            await w_conn.execute(
                """
                INSERT INTO calls (id, campaign_id, borrower_id, agent_id, state, created_at, updated_at, version)
                VALUES (?, 'camp-crash', 'borr-crash-1', 'agent-crash-1', 'QUEUED', ?, ?, 1)
                """,
                (call_id, time.time(), time.time())
            )
            await w_conn.commit()

            # 2. Reserve Agent & Commit RESERVED write
            await AgentStateMachine.reserve_agent(w_conn, "agent-crash-1", call_id=call_id)
            await CallStateMachine.transition(w_conn, call_id, CallState.RESERVED, agent_id="agent-crash-1")
            
            logger.info(">>> Worker Task: RESERVED state write COMMITTED to SQLite database.")
            reserved_committed_event.set()

            # Wait here before writing INITIATED to allow task cancellation to strike
            await asyncio.sleep(10.0)

            # (This line will never be reached due to cancellation)
            await CallStateMachine.transition(w_conn, call_id, CallState.INITIATED)
        finally:
            await w_conn.close()

    # Launch worker task
    logger.info("Step 1: Spawning async worker dialing task...")
    task = asyncio.create_task(worker_dial_task())

    # Wait until RESERVED write commits
    await reserved_committed_event.wait()
    logger.info("Step 2: RESERVED write verified in DB.")

    # Trigger real task cancellation BEFORE INITIATED write commits
    logger.info("Step 3: Triggering real task.cancel() on worker task mid-flight...")
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        logger.info("Step 4: Worker task CANCELLED mid-execution! Task is dead.")

    # Verify half-committed state in DB
    async with conn.execute("SELECT state FROM agents WHERE id = 'agent-crash-1'") as cursor:
        a_row = await cursor.fetchone()
        logger.info(f"DB Check - Agent state: {a_row['state']} (Expect RESERVED)")
        assert a_row["state"] == AgentState.RESERVED.value

    async with conn.execute("SELECT state FROM calls WHERE id = ?", (call_id,)) as cursor:
        c_row = await cursor.fetchone()
        logger.info(f"DB Check - Call state: {c_row['state']} (Expect RESERVED)")
        assert c_row["state"] == CallState.RESERVED.value

    # Run Reconciliation Engine
    logger.info("Step 5: Executing Recovery Engine reconciliation tick...")
    recovery_engine = WorkerCrashRecoveryEngine(orphan_timeout_seconds=0.001)
    await asyncio.sleep(0.01)
    rec_result = await recovery_engine.reconcile_orphans(conn)
    logger.info(f"Reconciliation Result: {rec_result}")

    # Verify state after recovery
    async with conn.execute("SELECT state FROM agents WHERE id = 'agent-crash-1'") as cursor:
        a_final = await cursor.fetchone()
        logger.info(f"Post-Recovery Agent State: {a_final['state']} (Expect AVAILABLE)")
        assert a_final["state"] == AgentState.AVAILABLE.value

    async with conn.execute("SELECT state, error_message FROM calls WHERE id = ?", (call_id,)) as cursor:
        c_final = await cursor.fetchone()
        logger.info(f"Post-Recovery Call State: {c_final['state']} (Expect FAILED)")
        assert c_final["state"] == CallState.FAILED.value
        assert "Worker crash" in c_final["error_message"]

    await conn.close()
    if os.path.exists(SIM_DB_PATH):
        try:
            os.remove(SIM_DB_PATH)
        except PermissionError:
            pass
    logger.info("=== WORKER CRASH SIMULATION SUCCESSFUL ===")

if __name__ == "__main__":
    asyncio.run(run_simulation())
