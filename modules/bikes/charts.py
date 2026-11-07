import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from core.database import get_pool

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


async def generate_station_chart(station_id: str, station_name: str = "Stacja") -> Optional[bytes]:
    pool = get_pool()
    cutoff = datetime.now(WARSAW_TZ) - timedelta(hours=24)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT timestamp, bikes 
            FROM station_snapshots 
            WHERE station_id = $1 AND timestamp >= $2 
            ORDER BY timestamp ASC;
            """,
            station_id,
            cutoff,
        )

    if not rows or len(rows) < 2:
        # Plansza informacyjna, jeśli mamy dopiero 1 próbkę
        fig, ax = plt.subplots(figsize=(6, 2.8), dpi=100)
        ax.text(
            0.5, 0.5,
            "Gromadzenie danych...\nSprawdź ponownie za chwilę.",
            ha="center", va="center", color="#6b7280"
        )
        ax.set_axis_off()
        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format="png")
        plt.close(fig)
        return buf.getvalue()

    times = [r["timestamp"] for r in rows]
    bikes = [r["bikes"] for r in rows]

    fig, ax = plt.subplots(figsize=(6, 2.8), dpi=110)
    ax.plot(times, bikes, color="#2563eb", linewidth=2)
    ax.fill_between(times, bikes, color="#3b82f6", alpha=0.15)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=WARSAW_TZ))
    ax.set_title(f"Historia rotacji: {station_name}", fontsize=10, fontweight="bold")
    ax.set_ylim(bottom=0, top=max(bikes) + 2)
    ax.grid(True, linestyle="--", alpha=0.5)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()