# 🏙️ Warsaw Multimodal Transit Analytics (Smart City GIS)

A high-performance spatial analytics and real-time telemetry engine for Warsaw public transit (trams) and bike-sharing infrastructure (Veturilo). The system integrates live GPS streaming, high-frequency map-matching in the Polish metric coordinate system **PUWG 1992 (EPSG:2180)** using **PostGIS**, native database-side GeoJSON serialization, and a custom multimodal scoring model for MaaS (Mobility as a Service) transfer hubs.

---

## 🚀 Key Features

* **Native PostGIS Map-Matching:** Real-time tram positions fetched from the City of Warsaw API are dynamically projected onto the rail track network (`tram_edges`) in EPSG:2180 using GiST spatial indexing (`<->`) and `ST_ClosestPoint`. Erroneous GPS readings deviating by more than 25 meters are automatically pruned (`ST_DWithin`).
* **Dwell Time Detection:** An event-driven state engine detects passenger boarding and alighting times across platform clusters (`tram_platforms`, 35 m buffer). Validated dwell times between **8.0 s and 180.0 s** are logged into `tram_dwell_events`, eliminating traffic light stops and terminus layovers.
* **Multimodal Hub Scoring:** A weighted analytical SQL model scores bike-sharing stations based on railway accessibility (buffer of 150–250 m), recent tram traffic intensity (past 2 hours), and immediate bike availability.
* **Platform-Level Analytics Dashboard:** Visualizes per-platform and per-line dwell statistics, sample counts, and stop cluster distributions directly in GeoJSON.
* **Low-I/O Delta Snapshotting:** The bike worker maintains a RAM cache of current stations and commits only state deltas (`bikes` count changes) to PostgreSQL, heavily optimizing database operations.
* **Dynamic Time-Series Charting:** Server-side `matplotlib` renders 24-hour bike rotation trendlines (`/analytics/station/{station_id}/chart.png`) injected on demand into interactive map popups.
* **Responsive MapLibre UI:** Modern glassmorphism UI with interactive layer controls, a bottom sheet for mobile viewports, and dedicated analytics pages.

---

## 🛠️ Architecture & Tech Stack

```text
                ┌──────────────────────────────────────────────┐
                │               External APIs                  │
                │     (City of Warsaw, CityBikes, OSM)         │
                └──────────────────────┬───────────────────────┘
                                       │ HTTP / Async Tasks
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       FastAPI Asynchronous Backend                          │
│                                                                             │
│  ┌───────────────────────┐ ┌──────────────────────┐ ┌────────────────────┐  │
│  │ tram_telemetry_poller │ │ bike_history_poller  │ │ tram_cleaner_task  │  │
│  │ (ZTM GPS -> PostGIS)  │ │ (RAM / Delta DB)     │ │ (1-hour retention) │  │
│  └───────────────────────┘ └──────────────────────┘ └────────────────────┘  │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │ asyncpg Connection Pool (min: 5, max: 20)
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         PostgreSQL + PostGIS (EPSG:2180)                    │
│                                                                             │
│  • tram_live_state (active vehicles) • cycleway_edges (OSM classified)      │
│  • tram_dwell_events (dwell archive) • veturilo_stations (live buffer)      │
│  • tram_edges & tram_platforms       • station_snapshots (temporal deltas)  │
│  • veturilo_station_infrastructure  (precomputed spatial relations)         │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │ Native GeoJSON generation in SQL
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      Frontend: MapLibre GL JS + RWD                         │
│  • /map                   - Live tracking & OSM bike infrastructure filters │
│  • /analytics/tram        - Per-stop & per-line passenger dwell analytics   │
│  • /analytics/leaderboard - Multimodal transit hub rankings                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 🧠 Core Algorithms & Analytical Models

### 1. Ingestion Pipeline & Dwell Time Detection

Telemetry data streams are matched against track geometries and platform clusters via an atomic CTE pipeline (`INGEST_TRAMS_SQL`):

1. **Spatial Snapping:** Incoming GPS coordinates (WGS84) are transformed to EPSG:2180 and snapped to the nearest railway edge using `tram_edges` spatial indexes (`<->`).
2. **Platform Association:** Vehicles within a 35-meter radius of a platform (`tram_platforms`) are flagged for potential boarding events.
3. **Dwell Duration Estimation:**
   * **Multi-ping Dwell** (>= 2 consecutive pings recorded at the platform):
     `dt = (t_prev_gps - t_dwell_start) + min(6.0, dt_sec * 0.4)`
   * **Single-ping Dwell** (stop handled within a single telemetry sampling window, 9.0s <= dt_sec <= 40.0s and displacement speed < 4.5 m/s):
     `dt = max(8.0, min(24.0, dt_sec - (dist / 7.0)))`
     > *Note:* 7.0 m/s represents the estimated departure/maneuvering speed (~ 25.2 km/h) used to deduct cruising travel time from the telemetry gap.
4. **Validation:** Evaluated events are committed to `tram_dwell_events` only if 8.0s <= duration_sec <= 180.0s.

### 2. Multimodal Hub Score Model

The ranking engine evaluates each station's efficiency as a transit interchange hub:

`HubScore = S_bikes + S_tram_dist + S_tram_dwells + S_cycleways - P_isolation`

* **Bike Availability (S_bikes):** 
  `min(free_bikes, 25) * 1.4` (max 35.0 pts)
* **Tram Proximity (S_tram_dist):** Linear decay within 250 m:
  `max(0.0, (250.0 - dist_to_tram_m) / 6.25)` (max 40.0 pts)
* **Tram Service Intensity (S_tram_dwells):** Verified passenger dwell operations over the past 2 hours:
  `min(tram_dwells_count * 1.5, 25.0)` (max 25.0 pts)
* **Safe Cycling Network (S_cycleways):** Weighted cycle infrastructure length within a 150 m buffer:
  `min(safe_network_meters / 15.0, 30.0)` (max 30.0 pts)
* **Isolation Penalty (P_isolation):** Penalty applied if the station is detached from the cycling network:
  `min(25.0, dist_to_cycleway_m - 25.0)` if dist > 25.0 m, otherwise `0.0`

---

## 📡 API Endpoints

### Bikeshare & Multimodal Hubs (`/bikes`, `/analytics`)
| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/bikes/citybikes/warsaw` | Real-time station GeoJSON served directly from server RAM cache. |
| `GET` | `/bikes/leaderboard?top_n=36` | Dynamic multimodal transit hub rankings computed via PostGIS scoring. |
| `GET` | `/analytics/station/{id}/chart.png` | 24-hour station occupancy chart generated as PNG via Matplotlib. |

### Tram Telemetry & Infrastructure (`/network/tram`)
| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/network/tram/live` | Real-time snapped vehicle positions in GeoJSON FeatureCollection format. |
| `GET` | `/network/tram/dwells/all-stops` | Aggregated stop dwell analytics (average dwell times, sample counts, line breakdown). |
| `GET` | `/network/tram/tracks` | Tramway rail network centerlines generated directly from PostGIS. |
| `GET` | `/network/tram/platforms` | Platform stop nodes with cluster and stop tag mappings. |
| `POST`| `/network/tram/sync` | Triggers background synchronization of tram tracks from OpenStreetMap. |

### Cycling Infrastructure (`/network`)
| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/network/safe-cycleways` | Classified safe bike infrastructure network (DDR, lanes, woonerfs, zone 30). |
| `POST`| `/network/safe-cycleways/sync` | Triggers background sync of cycling paths from Overpass OSM across Warsaw tiles. |

---

## ⚙️ Setup & Deployment

### 1. Database (Docker Compose)
Start the PostGIS and pgweb services:
```bash
docker compose up -d
```

### 2. Environment Variables (`.env`)
Create a `.env` file in the project root:
```env
DATABASE_URL=postgresql://gis_user:gis_secure_password_123@localhost:5432/warsaw_transit_gis
WAW_API_KEY=your_api_key_from_api_um_warszawa
```

### 3. Run Application
```bash
pip install -r requirements.txt
python main.py
```
The server will initialize connection pools, launch background telemetry workers, and serve the dashboard at `http://localhost:8000/map`.