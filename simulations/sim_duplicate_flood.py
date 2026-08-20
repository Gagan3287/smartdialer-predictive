import asyncio
import os
import time
import logging
from app.db.database import get_db_connection, init_db
from app.state_machines.call_state import CallLockRegistry, CallState, CallStateMachine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sim_duplicate_flood")

SIM_DB_PATH = "sim_duplicate_flood.db"

async def run_simulation():
    os.environ["SMARTDIALER_DB_PATH"] = SIM_DB_PATH
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

    call_id = "call-dup-flood-1"
    await conn.execute(
        """
        INSERT INTO calls (id, campaign_id, borrower_id, state, created_at, updated_at, version)
        VALUES (?, 'camp-dup', 'borr-1', 'QUEUED', ?, ?, 1)
        """,
        (call_id, now, now)
    )
    await conn.commit()

    # Move to RINGING
    await CallStateMachine.transition(conn, call_id, CallState.RESERVED)
    await CallStateMachine.transition(conn, call_id, CallState.INITIATED)
    await CallStateMachine.transition(conn, call_id, CallState.RINGING)

    logger.info("Flooding 100 duplicate RINGING events for active call...")
    duplicate_executed_count = 0
    noop_count = 0

    for i in range(100):
        res, executed = await CallStateMachine.transition(conn, call_id, CallState.RINGING)
        if executed:
            duplicate_executed_count += 1
        else:
            noop_count += 1

    logger.info(f"Duplicate Flood Result - Executed Transitions: {duplicate_executed_count}, Idempotent No-Ops: {noop_count}")
    assert duplicate_executed_count == 0
    assert noop_count == 100

    async with conn.execute("SELECT state, version FROM calls WHERE id = ?", (call_id,)) as cursor:
        row = await cursor.fetchone()
        logger.info(f"Call State after flood: {row['state']} (Version: {row['version']})")
        assert row["state"] == CallState.RINGING.value

    await conn.close()
    if os.path.exists(SIM_DB_PATH):
        try:
            os.remove(SIM_DB_PATH)
        except PermissionError:
            pass
    logger.info("=== DUPLICATE EVENT FLOOD SIMULATION SUCCESSFUL ===")

if __name__ == "__main__":
    asyncio.run(run_simulation())
