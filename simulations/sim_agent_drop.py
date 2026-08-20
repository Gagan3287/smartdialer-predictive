import asyncio
import os
import time
import logging
from app.db.database import get_db_connection, init_db
from app.state_machines.agent_state import AgentState, AgentStateMachine, AgentLockRegistry
from app.state_machines.call_state import CallLockRegistry
from app.providers.provider_a import ProviderA
from app.pacing.pacing_engine import PredictivePacingEngine
from app.safety.safety_controller import SafetyController
from app.allocator.call_allocator import CallAllocator
from app.progressive.progressive_dialer import ProgressiveDialer
from app.campaign.campaign_manager import CampaignManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sim_agent_drop")

SIM_DB_PATH = "sim_agent_drop.db"

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

    # Seed 100 agents (AVAILABLE) and 200 borrowers
    logger.info("Seeding 100 available agents and 200 borrowers...")
    for i in range(1, 101):
        await conn.execute(
            "INSERT INTO agents (id, name, state, version, updated_at) VALUES (?, ?, ?, 1, ?)",
            (f"agent-{i}", f"Agent {i}", AgentState.AVAILABLE.value, now)
        )
    for i in range(1, 201):
        await conn.execute(
            "INSERT INTO borrowers (id, name, phone_number, attempts, status, updated_at) VALUES (?, ?, ?, 0, 'PENDING', ?)",
            (f"borr-{i}", f"Borrower {i}", f"+1555222{i:03d}", now)
        )
    await conn.commit()

    provider = ProviderA(failure_rate=0.0, setup_latency=0.01)
    pacing = PredictivePacingEngine()
    prog_dialer = ProgressiveDialer(provider=provider)
    safety = SafetyController(target_abandon_rate=0.03, progressive_dialer=prog_dialer)
    allocator = CallAllocator(provider=provider)
    campaign = CampaignManager(pacing, safety, allocator, prog_dialer)

    # Initial Pacing Tick with 100 agents
    logger.info("Executing Pacing Tick 1 with 100 available agents...")
    t0 = time.time()
    res1 = await campaign.execute_pacing_tick(conn, "camp-drop")
    logger.info(f"Tick 1 Output - Recommended: {res1['pacing']['recommended_calls']}, Safety Approved: {res1['safety']['approved_calls']}")

    # Sudden Agent Drop: 40 agents log off (state -> OFFLINE)
    logger.info("SUDDEN AGENT DROP: 40 agents logging off within milliseconds (100 -> 60)...")
    drop_t0 = time.time()
    for i in range(1, 41):
        await AgentStateMachine.transition(conn, f"agent-{i}", AgentState.OFFLINE)
    drop_latency_ms = (time.time() - drop_t0) * 1000
    logger.info(f"Agent state updates completed in {drop_latency_ms:.2f} ms.")

    # Pacing Tick 2 immediately after agent drop
    logger.info("Executing Pacing Tick 2 immediately after agent drop...")
    t2_start = time.time()
    res2 = await campaign.execute_pacing_tick(conn, "camp-drop")
    reaction_latency_ms = (time.time() - t2_start) * 1000

    logger.info(
        f"Tick 2 Output - Available Agents: {res2['metrics']['available_agents']}, "
        f"Recommended: {res2['pacing']['recommended_calls']}, "
        f"Safety Approved: {res2['safety']['approved_calls']}, "
        f"Reaction Latency: {reaction_latency_ms:.2f} ms"
    )

    # Assert Pacing and Safety immediately scaled down call recommendations
    assert res2["metrics"]["available_agents"] == 60
    assert res2["safety"]["approved_calls"] <= 60
    logger.info("=== SUDDEN AGENT DROP SIMULATION SUCCESSFUL ===")

    await conn.close()
    if os.path.exists(SIM_DB_PATH):
        try:
            os.remove(SIM_DB_PATH)
        except PermissionError:
            pass

if __name__ == "__main__":
    asyncio.run(run_simulation())
