import os
from pathlib import Path

# Ścieżki do baz SQLite
RESOURCES_DIR = Path(__file__).parent / "resources"
CYCLEWAYS_DB_PATH = RESOURCES_DIR / "cycleways_network.db"
TRAM_DB_PATH = RESOURCES_DIR / "tram_network.db"

# Koordynaty bazowe i kafelki dla Overpass
DEFAULT_WARSAW_BBOX = "52.09,20.85,52.37,21.27"

WARSAW_BIKE_TILES = [
    {
        "name": "SW (Mokotów, Ochota, Ursynów)",
        "bbox": "52.09,20.85,52.23,21.05",
    },
    {
        "name": "SE (Praga Płd, Wilanów, Wawer)",
        "bbox": "52.09,21.05,52.23,21.27",
    },
    {
        "name": "NW (Wola, Bemowo, Bielany)",
        "bbox": "52.23,20.85,52.37,21.05",
    },
    {
        "name": "NE (Śródmieście, Praga Płn, Białołęka)",
        "bbox": "52.23,21.05,52.37,21.27",
    },
]

# Serwery Overpass API
OVERPASS_SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Nagłówki HTTP dla zapytań GIS
OVERPASS_HEADERS = {
    "User-Agent": "WarsawTransitGIS/1.0 (Urban research & transit routing)",
    "Accept": "application/json",
}

# Konfiguracja API Warszawy pod kątem telemetrii na żywo
WAW_API_KEY = os.getenv("WAW_API_KEY", "")
WAW_TRAM_RESOURCE_ID = "f2e5503e-927d-4ad3-9500-4ab9e55deb59"
WAW_API_URL = "https://api.um.warszawa.pl/api/action/busestrams_get/"