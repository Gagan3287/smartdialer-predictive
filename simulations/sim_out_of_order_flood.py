import asyncio
import os
import time
import logging
from app.db.database import get_db_connection, init_db
from app.state_machines.call_state import CallLockRegistry, CallState, CallStateMachine

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sim_out_of_order_flood")

SIM_DB_PATH = "sim_out_of_order_flood.db"

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

    call_id = "call-ooo-flood-1"
    await conn.execute(
        """
        INSERT INTO calls (id, campaign_id, borrower_id, state, created_at, updated_at, version)
        VALUES (?, 'camp-ooo', 'borr-1', 'QUEUED', ?, ?, 1)
        """,
        (call_id, now, now)
    )
    await conn.commit()

    logger.info("Step 1: Out-of-order COMPLETED event arrives while call is in QUEUED state...")
    res, executed = await CallStateMachine.transition(conn, call_id, CallState.COMPLETED)
    logger.info(f"Transition result: executed={executed}, state={res['state']}")
    assert res["state"] == CallState.COMPLETED.value

    logger.info("Step 2: Late RINGING & ANSWERED events arrive after COMPLETED...")
    res_ring, exec_ring = await CallStateMachine.transition(conn, call_id, CallState.RINGING)
    res_ans, exec_ans = await CallStateMachine.transition(conn, call_id, CallState.ANSWERED)

    logger.info(f"Late RINGING handled: executed={exec_ring}, state={res_ring['state']}")
    logger.info(f"Late ANSWERED handled: executed={exec_ans}, state={res_ans['state']}")

    assert exec_ring is False
    assert exec_ans is False
    assert res_ans["state"] == CallState.COMPLETED.value

    await conn.close()
    if os.path.exists(SIM_DB_PATH):
        try:
            os.remove(SIM_DB_PATH)
        except PermissionError:
            pass
    logger.info("=== OUT-OF-ORDER EVENT FLOOD SIMULATION SUCCESSFUL ===")

if __name__ == "__main__":
    asyncio.run(run_simulation())
