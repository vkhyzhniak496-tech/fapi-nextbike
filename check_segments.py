import sqlite3
from pathlib import Path

db_corridors = Path("resources/tram_corridors.db")
if not db_corridors.exists():
    print("Brak pliku resources/tram_corridors.db!")
    exit()

with sqlite3.connect(db_corridors) as conn:
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM tram_direct_segments;")
    print(f"Liczba wszystkich segmentow w bazie: {cur.fetchone()[0]}")

    cur.execute("""
        SELECT line, from_cluster, to_cluster, avg_duration_sec, samples_count 
        FROM tram_direct_segments 
        WHERE from_cluster LIKE '%Mangalia%' OR to_cluster LIKE '%Mangalia%'
           OR from_cluster LIKE '%Dolna%' OR to_cluster LIKE '%Dolna%'
        LIMIT 20;
    """)
    rows = cur.fetchall()
    print("\nPrzykladowe segmenty wokol Mangalia / Dolna:")
    for r in rows:
        print(f"Linia {r['line']}: {r['from_cluster']} -> {r['to_cluster']} ({r['avg_duration_sec']}s, probki: {r['samples_count']})")
