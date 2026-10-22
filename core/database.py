import os
from typing import Optional
import asyncpg
from dotenv import load_dotenv

# Wczytanie pliku .env zanim odpytamy os.getenv
load_dotenv()

# Pobieranie konfiguracji z env lub domyślny kontener lokalny Docker
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://postgres:postgres@localhost:5432/warsaw_gis"
)

_DB_POOL: Optional[asyncpg.Pool] = None


async def init_db_pool() -> asyncpg.Pool:
    """Inicjalizuje globalną asynchroniczną pulę połączeń PostgreSQL."""
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
    """Zamyka asynchroniczną pulę połączeń przy zamykaniu aplikacji."""
    global _DB_POOL
    if _DB_POOL is not None:
        await _DB_POOL.close()
        _DB_POOL = None


def get_pool() -> asyncpg.Pool:
    """Zwraca aktywną pulę połączeń asyncpg."""
    if _DB_POOL is None:
        raise RuntimeError("Pula bazy danych PostgreSQL nie została zainicjalizowana!")
    return _DB_POOL