from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = BASE_DIR / "resources"


class Settings(BaseSettings):
    # Klucz miejski API (kompatybilny z obiema konwencjami nazw)
    WAW_API_KEY: str = ""

    @property
    def UM_WARSZAWA_API_KEY(self) -> str:
        return self.WAW_API_KEY

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()

# Stałe API Warszawy (ZTM Telemetria)
UM_WARSZAWA_VEHICLES_URL = "https://api.um.warszawa.pl/api/action/busestrams_get/"
UM_WARSZAWA_RESOURCE_ID = "f2e5503e-927d-4ad3-9500-4ab9e55deb59"

# Bounding Box Warszawy (min_lat, min_lon, max_lat, max_lon)
DEFAULT_WARSAW_BBOX = "52.09,20.85,52.37,21.27"

# Klastry serwerów Overpass (od najbardziej niezawodnych)
OVERPASS_SERVERS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

# Nagłówki HTTP
OVERPASS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WarsawTransitGIS/2.0",
    "Accept": "application/json",
}

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}