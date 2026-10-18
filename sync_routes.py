import os
import asyncio
import httpx
from dotenv import load_dotenv

# Importujemy bezpośrednio z Twojego modułu bazy danych:
from core.database import init_db_pool, close_db_pool

load_dotenv()

API_KEY = (
    os.getenv("ZTM_API_KEY") 
    or os.getenv("WAW_API_KEY") 
    or os.getenv("UM_WARSZAWA_API_KEY") 
    or os.getenv("API_KEY")
)

ROUTES_API_URL = "https://api.um.warszawa.pl/api/action/public_transport_routes/"


async def init_table(conn):
    """Tworzy tabelę oficjalnych wzorców tras ZTM."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tram_route_patterns (
            line TEXT NOT NULL,
            route_variant TEXT NOT NULL,
            stop_sequence INT NOT NULL,
            cluster_id TEXT NOT NULL,
            stop_nr TEXT NOT NULL,
            stop_tag TEXT NOT NULL,
            street_id TEXT,
            distance_m INT NOT NULL,
            stop_type INT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (line, route_variant, stop_sequence)
        );

        CREATE INDEX IF NOT EXISTS idx_tram_route_lookup 
            ON tram_route_patterns (line, route_variant);

        CREATE INDEX IF NOT EXISTS idx_tram_route_stops 
            ON tram_route_patterns (cluster_id, stop_nr);
    """)


async def fetch_and_sync_routes():
    if not API_KEY:
        raise ValueError("Nie znaleziono klucza API ZTM w pliku .env!")

    print("Pobieranie wzorców tras z oficjalnego API UM Warszawa (ZTM)...")
    async with httpx.AsyncClient(timeout=45.0) as client:
        resp = await client.get(ROUTES_API_URL, params={"apikey": API_KEY})
        if resp.status_code != 200:
            raise RuntimeError(f"Błąd API UM Warszawa: HTTP {resp.status_code}")
        data = resp.json()

    routes_data = data.get("result", {})
    records = []

    # Wybieramy tylko linie tramwajowe (1 - 79)
    for line_nr, variants in routes_data.items():
        if not line_nr.isdigit() or int(line_nr) >= 100:
            continue

        for variant_name, stops in variants.items():
            for seq_str, stop_info in stops.items():
                cluster_id = str(stop_info.get("nr_zespolu", "")).strip()
                stop_nr = str(stop_info.get("nr_przystanku", "")).strip()
                street_id = str(stop_info.get("ulica_id", "")).strip()
                distance_m = int(stop_info.get("odleglosc", 0))
                stop_type = int(stop_info.get("typ", 0))

                records.append((
                    line_nr,
                    variant_name,
                    int(seq_str),
                    cluster_id,
                    stop_nr,
                    f"{cluster_id}{stop_nr}",
                    street_id,
                    distance_m,
                    stop_type
                ))

    print(f"Wyciągnięto {len(records)} punktów tras dla tramwajów. Zapisuję do PostgreSQL...")

    pool = await init_db_pool()
    try:
        async with pool.acquire() as conn:
            await init_table(conn)

            await conn.executemany("""
                INSERT INTO tram_route_patterns (
                    line, route_variant, stop_sequence, cluster_id, stop_nr, stop_tag, street_id, distance_m, stop_type, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CURRENT_TIMESTAMP)
                ON CONFLICT (line, route_variant, stop_sequence) DO UPDATE SET
                    cluster_id = EXCLUDED.cluster_id,
                    stop_nr = EXCLUDED.stop_nr,
                    stop_tag = EXCLUDED.stop_tag,
                    street_id = EXCLUDED.street_id,
                    distance_m = EXCLUDED.distance_m,
                    stop_type = EXCLUDED.stop_type,
                    updated_at = CURRENT_TIMESTAMP;
            """, records)

        print("Gotowe! Oficjalne trasy tramwajowe ZTM zostały zsynchronizowane w bazie.")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(fetch_and_sync_routes())