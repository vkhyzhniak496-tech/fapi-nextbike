import os
from typing import Optional
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL"
)

_DB_POOL: Optional[asyncpg.Pool] = None


async def init_db_pool() -> asyncpg.Pool:
    """Inicjalizuje globalną pulę połączeń z rejestracją typów PostGIS/JSON."""
    global _DB_POOL
    if _DB_POOL is None:
        _DB_POOL = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=5,
            max_size=20,
            command_timeout=60.0
        )
    return _DB_POOL


async def close_db_pool() -> None:
    global _DB_POOL
    if _DB_POOL is not None:
        await _DB_POOL.close()
        _DB_POOL = None


def get_pool() -> asyncpg.Pool:
    if _DB_POOL is None:
        raise RuntimeError("Pula bazy danych PostgreSQL/PostGIS nie została zainicjalizowana!")
    return _DB_POOL