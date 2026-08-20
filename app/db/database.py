import aiosqlite
import os

DB_PATH = os.getenv("SMARTDIALER_DB_PATH", "smartdialer.db")

async def get_db_connection() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    return conn

async def init_db():
    conn = await get_db_connection()
    try:
        # Agents Table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'OFFLINE',
                version INTEGER NOT NULL DEFAULT 1,
                current_call_id TEXT,
                updated_at REAL NOT NULL
            );
        """)
        
        # Borrowers Table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS borrowers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                phone_number TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'PENDING',
                updated_at REAL NOT NULL
            );
        """)

        # Calls Table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                borrower_id TEXT NOT NULL,
                agent_id TEXT,
                state TEXT NOT NULL DEFAULT 'QUEUED',
                provider_id TEXT,
                provider_call_id TEXT,
                error_message TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            );
        """)

        # Campaigns Table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'PREDICTIVE',
                status TEXT NOT NULL DEFAULT 'PAUSED',
                target_abandon_rate REAL NOT NULL DEFAULT 0.03,
                updated_at REAL NOT NULL
            );
        """)

        # Audit Logs Table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                event_name TEXT NOT NULL,
                details TEXT NOT NULL
            );
        """)
        await conn.commit()
    finally:
        await conn.close()
