import os
import asyncio
import httpx
from dotenv import load_dotenv
from core.database import init_db_pool, close_db_pool

load_dotenv()

API_KEY = os.getenv("ZTM_API_KEY") or os.getenv("WAW_API_KEY") or os.getenv("API_KEY")
STOPS_DICT_URL = "https://api.um.warszawa.pl/api/action/dbtimetable_get"
# Prawidłowy, scalony UUID zasobu listy przystanków z ZTM:
STOPS_RESOURCE_ID = "ab75c33d-3a26-4342-b36a-6e5fef0a3ac3"


async def sync_stops_dictionary():
    if not API_KEY:
        raise ValueError("Brak klucza API ZTM w pliku .env!")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    params = {
        "id": STOPS_RESOURCE_ID,
        "apikey": API_KEY.strip()
    }

    print("Pobieranie oficjalnego słownika przystanków ZTM...")
    async with httpx.AsyncClient(timeout=90.0, follow_redirects=True) as client:
        resp = await client.get(STOPS_DICT_URL, params=params, headers=headers)
        
        print(f"Status HTTP: {resp.status_code}")
        
        if resp.status_code != 200:
            print(f"Treść błędu z serwera: {resp.text[:500]}")
            return

        try:
            data = resp.json()
        except Exception as e:
            print("Serwer nie zwrócił poprawnego JSON-a!")
            print(f"Pierwsze 300 znaków odpowiedzi:\n{resp.text[:300]}")
            raise e

    # ZTM czasami zwraca błąd w polu JSON: {"error": "..."} lub {"result": "false"}
    if isinstance(data.get("result"), str):
        print(f"Komunikat z API ZTM: {data.get('result')}")
        return

    raw_list = data.get("result", [])
    if not raw_list:
        print("Pusta lista przystanków w odpowiedzi API.")
        return

    records = []
    for item in raw_list:
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
        except (ValueError, TypeError):
            continue

        stop_tag = f"{cluster_id}{stop_nr}"
        records.append((cluster_id, stop_nr, stop_tag, name, lon, lat))

    print(f"Pobrano i sparsowano {len(records)} słupków przystankowych. Zapis do PostgreSQL...")

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

        print("Oficjalny słownik przystanków ZTM został pomyślnie zsynchronizowany!")
    finally:
        await close_db_pool()


if __name__ == "__main__":
    asyncio.run(sync_stops_dictionary())