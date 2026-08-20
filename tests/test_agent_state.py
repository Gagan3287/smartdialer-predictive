import asyncio
import os
import pytest
import pytest_asyncio
import time
from app.db.database import get_db_connection, init_db, DB_PATH
from app.state_machines.agent_state import (
    AgentState,
    AgentStateMachine,
    AgentLockRegistry,
    AgentNotAvailableError,
    OptimisticLockError,
    InvalidStateTransitionError
)
from app.state_machines.call_state import CallLockRegistry
from app.providers.provider_a import ProviderA
from app.progressive.progressive_dialer import ProgressiveDialer

TEST_DB_PATH = "test_smartdialer.db"

@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(monkeypatch):
    monkeypatch.setattr("app.db.database.DB_PATH", TEST_DB_PATH)
    AgentLockRegistry.reset()
    CallLockRegistry.reset()
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass
    await init_db()
    yield
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except PermissionError:
            pass

@pytest.mark.asyncio
async def test_agent_state_invalid_transition():
    conn = await get_db_connection()
    try:
        now = time.time()
        await conn.execute(
            "INSERT INTO agents (id, name, state, version, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("agent-1", "Alice", AgentState.OFFLINE.value, 1, now)
        )
        await conn.commit()

        with pytest.raises(InvalidStateTransitionError):
            await AgentStateMachine.transition(conn, "agent-1", AgentState.CONNECTED)
    finally:
        await conn.close()

@pytest.mark.asyncio
async def test_50_concurrent_reservation_attempts():
    conn = await get_db_connection()
    try:
        now = time.time()
        await conn.execute(
            "INSERT INTO agents (id, name, state, version, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("agent-target", "Target Agent", AgentState.AVAILABLE.value, 1, now)
        )
        await conn.commit()
    finally:
        await conn.close()

    successes = []
    failures = []

    async def attempt_reservation(task_id: int):
        conn_local = await get_db_connection()
        try:
            res = await AgentStateMachine.reserve_agent(
                conn_local, "agent-target", call_id=f"call-{task_id}"
            )
            successes.append(task_id)
        except (AgentNotAvailableError, OptimisticLockError) as e:
            failures.append((task_id, str(e)))
        finally:
            await conn_local.close()

    tasks = [attempt_reservation(i) for i in range(50)]
    await asyncio.gather(*tasks)

    assert len(successes) == 1, f"Expected exactly 1 winner, got {len(successes)}"
    assert len(failures) == 49, f"Expected 49 failures, got {len(failures)}"

    conn_verify = await get_db_connection()
    try:
        async with conn_verify.execute("SELECT state, current_call_id FROM agents WHERE id = ?", ("agent-target",)) as cursor:
            row = await cursor.fetchone()
            assert row["state"] == AgentState.RESERVED.value
            assert row["current_call_id"] == f"call-{successes[0]}"
    finally:
        await conn_verify.close()

@pytest.mark.asyncio
async def test_progressive_dialer_never_exceeds_available_agents_under_concurrent_load():
    conn = await get_db_connection()
    try:
        now = time.time()
        # Insert 3 available agents
        for i in range(1, 4):
            await conn.execute(
                "INSERT INTO agents (id, name, state, version, updated_at) VALUES (?, ?, ?, ?, ?)",
                (f"agent-{i}", f"Agent {i}", AgentState.AVAILABLE.value, 1, now)
            )
        # Insert 20 borrowers
        for i in range(1, 21):
            await conn.execute(
                "INSERT INTO borrowers (id, name, phone_number, attempts, status, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (f"borr-{i}", f"Borrower {i}", f"+1555000{i:02d}", 0, "PENDING", now)
            )
        await conn.commit()
    finally:
        await conn.close()

    provider = ProviderA(failure_rate=0.0, setup_latency=0.05)
    dialer = ProgressiveDialer(provider=provider)

    results = []

    async def concurrent_dial_attempt(task_id: int):
        conn_local = await get_db_connection()
        try:
            res = await dialer.execute_progressive_dial(conn_local, campaign_id="camp-test")
            if res:
                results.append(res)
        finally:
            await conn_local.close()

    # Launch 50 concurrent progressive dial attempts against only 3 available agents
    tasks = [concurrent_dial_attempt(i) for i in range(50)]
    await asyncio.gather(*tasks)

    # 1. Assert total successful calls initiated is AT MOST 3 (matching available agents)
    assert len(results) <= 3, f"Over-allocation detected! {len(results)} calls initiated for 3 agents."

    # 2. Verify in DB that no more than 3 agents are reserved/dialing
    conn_verify = await get_db_connection()
    try:
        async with conn_verify.execute(
            "SELECT COUNT(*) as cnt FROM agents WHERE state IN ('RESERVED', 'DIALING', 'CONNECTED')"
        ) as cursor:
            row = await cursor.fetchone()
            active_agents = row["cnt"]
            assert active_agents <= 3
            assert active_agents == len(results)
    finally:
        await conn_verify.close()
