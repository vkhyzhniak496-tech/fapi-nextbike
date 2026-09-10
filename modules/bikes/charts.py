from datetime import datetime
import io
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def generate_step_chart_bytes(
    series: List[Dict[str, Any]], station_name: str
) -> bytes:
  """Rysuje wykres schodkowy dostępności rowerów w buforze pamięci RAM."""
  dates = [datetime.fromisoformat(row["timestamp"]) for row in series]
  values = [row["bikes"] for row in series]

  fig, ax = plt.subplots(figsize=(6.5, 3.0), dpi=100)

  ax.step(
      dates,
      values,
      where="post",
      color="#007cbf",
      marker="o",
      markersize=3.0,
      linewidth=1.5,
  )

  # Formatowanie osi X względem horyzontu czasowego
  delta_time = dates[-1] - dates[0]
  time_fmt = (
      mdates.DateFormatter("%d.%m %H:%M", tz=WARSAW_TZ)
      if delta_time.days >= 1
      else mdates.DateFormatter("%H:%M", tz=WARSAW_TZ)
  )

  ax.xaxis.set_major_formatter(time_fmt)
  ax.xaxis.set_major_locator(mdates.AutoDateLocator(tz=WARSAW_TZ))

  ax.set_title(
      f"Historia zmian: {station_name}", fontsize=10, fontweight="bold", pad=8
  )
  ax.set_ylabel("Liczba rowerów", fontsize=8)
  ax.grid(True, linestyle="--", alpha=0.4)

  ax.yaxis.set_major_locator(MaxNLocator(integer=True))
  min_v, max_v = min(values), max(values)
  ax.set_ylim(max(0, min_v - 1), max_v + 2)

  plt.xticks(rotation=20, fontsize=8)
  plt.yticks(fontsize=8)
  plt.tight_layout()

  buf = io.BytesIO()
  plt.savefig(buf, format="png")
  plt.close(fig)
  buf.seek(0)
  return buf.getvalue()