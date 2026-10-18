import os
import asyncio
import httpx
from dotenv import load_dotenv
from core.database import init_db_pool, close_db_pool

load_dotenv()

API_KEY = os.getenv("ZTM_API_KEY") or os.getenv("WAW_API_KEY") or os.getenv("API_KEY")
STOPS_DICT_URL = "https://api.um.warszawa.pl/api/action/dbtimetable_get"
STOPS_RESOURCE_ID = "ab75c33d-3a26-4342-b36a-6e5fef0a3ac3"


async def sync_stops_dictionary():
    if not API_KEY:
        raise ValueError("Brak klucza API ZTM w pliku .env!")

    print("Pobieranie oficjalnego słownika przystanków ZTM...")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            STOPS_DICT_URL,
            params={"id": STOPS_RESOURCE_ID, "apikey": API_KEY}
        )
        data = resp.json()

    raw_list = data.get("result", [])
    records = []

    for item in raw_list:
        # ZTM zwraca listę obiektów w formacie [{'key': 'zespol', 'value': '...'}, ...]
        val_map = {entry["key"]: entry["value"] for entry in item.get("values", [])}
        
        cluster_id = str(val_map.get("zespol", "")).strip()
        stop_nr = str(val_map.get("slupek", "")).strip()
        name = str(val_map.get("nazwa_zespolu", "")).strip()
        lat_str = val_map.get("szer_geo")
        lon_str = val_map.get("dlug_geo")

        if not cluster_id or not stop_nr or not lat_str or not lon_str:
            continue

        try:
            lat = float(lat_str)
            lon = float(lon_str)
            if lat == 0.0 or lon == 0.0:
                continue
        except ValueError:
            continue

        stop_tag = f"{cluster_id}{stop_nr}"
        records.append((cluster_id, stop_nr, stop_tag, name, lon, lat))

    print(f"Pobrano {len(records)} słupków przystankowych. Zapis do PostgreSQL...")

    pool = await init_db_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tram_stops_dictionary (
                    cluster_id TEXT NOT NULL,
                    stop_nr TEXT NOT NULL,
                    stop_tag TEXT PRIMARY KEY,
                    stop_name TEXT NOT NULL,
                    lon DOUBLE PRECISION NOT NULL,
                    lat DOUBLE PRECISION NOT NULL,
                    geom_2180 GEOMETRY(Point, 2180),
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_stops_dict_cluster 
                    ON tram_stops_dictionary (cluster_id);
                CREATE INDEX IF NOT EXISTS idx_stops_dict_geom 
                    ON tram_stops_dictionary USING GIST (geom_2180);
            """)

            await conn.executemany("""
                INSERT INTO tram_stops_dictionary (
                    cluster_id, stop_nr, stop_tag, stop_name, lon, lat, geom_2180, updated_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6,
                    ST_Transform(ST_SetSRID(ST_MakePoint($5, $6), 4326), 2180),
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (stop_tag) DO UPDATE SET
                    stop_name = EXCLUDED.stop_name,
                    lon = EXCLUDED.lon,
                    lat = EXCLUDED.lat,
                    geom_2180 = EXCLUDED.geom_2180,
                    updated_at = CURRENT_TIMESTAMP;
            """, records)

        print("Oficjalny słownik przystanków został zapisany.")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(sync_stops_dictionary())