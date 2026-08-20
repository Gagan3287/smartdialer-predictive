import asyncio
import os
import time
import logging
from app.db.database import get_db_connection, init_db
from app.state_machines.agent_state import AgentState, AgentStateMachine, AgentLockRegistry
from app.state_machines.call_state import CallLockRegistry, CallState, CallStateMachine
from app.providers.provider_a import ProviderA
from app.pacing.pacing_engine import PredictivePacingEngine
from app.safety.safety_controller import SafetyController, SafetyAction
from app.allocator.call_allocator import CallAllocator
from app.progressive.progressive_dialer import ProgressiveDialer
from app.campaign.campaign_manager import CampaignManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sim_provider_outage")

SIM_DB_PATH = "sim_provider_outage.db"

async def run_simulation():
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

    # Seed 10 agents and 50 borrowers
    for i in range(1, 11):
        await conn.execute(
            "INSERT INTO agents (id, name, state, version, updated_at) VALUES (?, ?, ?, 1, ?)",
            (f"agent-{i}", f"Agent {i}", AgentState.AVAILABLE.value, now)
        )
    for i in range(1, 51):
        await conn.execute(
            "INSERT INTO borrowers (id, name, phone_number, attempts, status, updated_at) VALUES (?, ?, ?, 0, 'PENDING', ?)",
            (f"borr-{i}", f"Borrower {i}", f"+1555111{i:02d}", now)
        )
    await conn.commit()

    provider = ProviderA(failure_rate=0.0, setup_latency=0.05)
    pacing = PredictivePacingEngine()
    prog_dialer = ProgressiveDialer(provider=provider)
    safety = SafetyController(target_abandon_rate=0.03, progressive_dialer=prog_dialer)
    allocator = CallAllocator(provider=provider)
    campaign = CampaignManager(pacing, safety, allocator, prog_dialer)

    logger.info("Phase 1: Running normal dialing ticks under healthy ProviderA...")
    res1 = await campaign.execute_pacing_tick(conn, "camp-outage")
    logger.info(f"Tick 1 - Action: {res1['safety']['action']}, Allocated: {res1['allocated_calls_count']}")

    logger.info("Phase 2: SWITCHING TELECOM PROVIDER TO 100% FAILURE OUTAGE...")
    provider.failure_rate = 1.0

    # Execute failed calls directly to populate historical failed outcomes in DB
    for i in range(1, 6):
        call_id = f"failed-call-{i}"
        await conn.execute(
            """
            INSERT INTO calls (id, campaign_id, borrower_id, agent_id, state, created_at, updated_at, version)
            VALUES (?, 'camp-outage', 'borr-1', 'agent-1', 'FAILED', ?, ?, 1)
            """,
            (call_id, time.time(), time.time())
        )
    await conn.commit()

    logger.info("Phase 3: Evaluating Safety Controller reaction after failed call accumulation...")
    res_outage = await campaign.execute_pacing_tick(conn, "camp-outage")
    logger.info(
        f"Outage Tick - Abandon Rate: {res_outage['metrics']['rolling_abandon_rate']:.2%}, "
        f"Safety Action: {res_outage['safety']['action']}, Reason: {res_outage['safety']['reason']}"
    )

    assert res_outage["safety"]["action"] == SafetyAction.FALLBACK_PROGRESSIVE.value
    logger.info("Safety Controller correctly triggered FALLBACK_PROGRESSIVE mode!")

    await conn.close()
    if os.path.exists(SIM_DB_PATH):
        try:
            os.remove(SIM_DB_PATH)
        except PermissionError:
            pass
    logger.info("=== PROVIDER OUTAGE SIMULATION SUCCESSFUL ===")

if __name__ == "__main__":
    asyncio.run(run_simulation())
