import os
import pytest
import pytest_asyncio
import time
from app.db.database import get_db_connection, init_db
from app.state_machines.call_state import (
    CallState,
    CallStateMachine,
    CallLockRegistry,
    InvalidCallStateTransitionError
)

TEST_DB_PATH = "test_smartdialer.db"

@pytest_asyncio.fixture(autouse=True)
async def setup_test_db(monkeypatch):
    monkeypatch.setattr("app.db.database.DB_PATH", TEST_DB_PATH)
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
async def test_call_state_transitions_and_idempotency():
    conn = await get_db_connection()
    try:
        now = time.time()
        call_id = "call-100"
        await conn.execute(
            """
            INSERT INTO calls (id, campaign_id, borrower_id, state, created_at, updated_at, version)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (call_id, "camp-1", "borr-1", CallState.QUEUED.value, now, now, 1)
        )
        await conn.commit()

        # Normal transition QUEUED -> RESERVED
        res, executed = await CallStateMachine.transition(conn, call_id, CallState.RESERVED)
        assert executed is True
        assert res["state"] == CallState.RESERVED.value

        # Duplicate transition RESERVED -> RESERVED (idempotent no-op)
        res_dup, executed_dup = await CallStateMachine.transition(conn, call_id, CallState.RESERVED)
        assert executed_dup is False
        assert res_dup["state"] == CallState.RESERVED.value

        # Transition RESERVED -> INITIATED -> RINGING
        await CallStateMachine.transition(conn, call_id, CallState.INITIATED)
        await CallStateMachine.transition(conn, call_id, CallState.RINGING)

        # Fast completion event (e.g. COMPLETED arrives)
        res_comp, executed_comp = await CallStateMachine.transition(conn, call_id, CallState.COMPLETED)
        assert executed_comp is True
        assert res_comp["state"] == CallState.COMPLETED.value

        # Late out-of-order ANSWERED event after COMPLETED arrives
        res_late, executed_late = await CallStateMachine.transition(conn, call_id, CallState.ANSWERED)
        assert executed_late is False
        assert res_late["state"] == CallState.COMPLETED.value
    finally:
        await conn.close()
