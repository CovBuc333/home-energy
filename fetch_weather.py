#!/usr/bin/env python3
"""Add daily mean outdoor temperature (°C) for home to site/data.json.

Stored under data["weather"]["daily_mean_c"] as {"YYYY-MM-DD": temp}.
Used to weather-adjust gas use (gas per heating degree day, base 15.5°C).

Source: Open-Meteo (free, no API key).
- First run back-fills from 2021-01-01 using the historical archive.
- Every run refreshes the last few weeks: recent days come from the
  forecast API's past_days (available the next day), and are later
  replaced by archive values once those are published (~5 days' lag).
Only complete days (before today, UK time) are stored.
A failure here never breaks the energy update; it just logs a warning.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

LAT, LON = 52.556, -1.823          # B72, Sutton Coldfield
LOCATION = "B72 (Sutton Coldfield)"
HISTORY_START = date(2021, 1, 1)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "data.json")
TZ = "Europe/London"


def fetch(base, **params):
    params.update(latitude=LAT, longitude=LON, daily="temperature_2m_mean", timezone=TZ)
    url = base + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        d = json.load(r)["daily"]
    return {t: v for t, v in zip(d["time"], d["temperature_2m_mean"]) if v is not None}


def main():
    with open(OUT) as f:
        data = json.load(f)

    today = datetime.now(ZoneInfo(TZ)).date()
    w = data.setdefault("weather", {})
    w["location"] = LOCATION
    w["source"] = "Open-Meteo"
    w["hdd_base_c"] = 15.5
    temps = w.setdefault("daily_mean_c", {})
    before = dict(temps)

    # Archive: back-fill any gap, and refresh the last ~3 weeks.
    arch_end = today - timedelta(days=6)
    missing = [HISTORY_START + timedelta(days=i)
               for i in range((arch_end - HISTORY_START).days + 1)
               if (HISTORY_START + timedelta(days=i)).isoformat() not in temps]
    arch_start = min(missing[0] if missing else arch_end, arch_end - timedelta(days=21))
    try:
        temps.update(fetch("https://archive-api.open-meteo.com/v1/archive",
                           start_date=arch_start.isoformat(), end_date=arch_end.isoformat()))
    except Exception as e:  # noqa: BLE001
        print(f"Warning: weather archive fetch failed: {e}", file=sys.stderr)

    # Recent days not yet in the archive.
    try:
        recent = fetch("https://api.open-meteo.com/v1/forecast", past_days=14, forecast_days=1)
        for k, v in recent.items():
            if k < today.isoformat() and (k > arch_end.isoformat() or k not in temps):
                temps[k] = v
    except Exception as e:  # noqa: BLE001
        print(f"Warning: recent weather fetch failed: {e}", file=sys.stderr)

    w["daily_mean_c"] = dict(sorted(temps.items()))
    if w["daily_mean_c"] != before:
        with open(OUT, "w") as f:
            json.dump(data, f, indent=1)
        print(f"Weather updated: {len(temps)} days, latest {max(temps) if temps else 'none'}.")
    else:
        print("Weather: no changes.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"Warning: weather update skipped: {e}", file=sys.stderr)
