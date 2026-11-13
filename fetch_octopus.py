#!/usr/bin/env python3
"""Pull monthly Octopus Energy usage and merge it into site/data.json.

History is never overwritten: a month already in data.json is only replaced
if it falls inside the last REFRESH_MONTHS months (recent months can be
incomplete or revised). Older months are only ever added when missing.
Git history keeps every earlier version of data.json as a backup.
"""
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.octopus.energy/v1"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site", "data.json")
REFRESH_MONTHS = 3


def get(url, auth):
    req = urllib.request.Request(url, headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def all_pages(url, auth):
    out = []
    while url:
        j = get(url, auth)
        out += j["results"]
        url = j.get("next")
    return out


def month_key(iso):
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(hours=12)
    d = d.astimezone(timezone.utc)
    return f"{d.year}-{d.month:02d}"


def find_meters(acct):
    found = {"electricity": [], "gas": []}
    for p in acct.get("properties", []):
        for mp in p.get("electricity_meter_points", []):
            if mp.get("is_export"):
                continue
            for m in mp.get("meters", []):
                found["electricity"].append(
                    f"electricity-meter-points/{mp['mpan']}/meters/{m['serial_number']}")
        for mp in p.get("gas_meter_points", []):
            for m in mp.get("meters", []):
                found["gas"].append(
                    f"gas-meter-points/{mp['mprn']}/meters/{m['serial_number']}")
    return found


def refresh_cutoff(today=None):
    today = today or datetime.now(timezone.utc)
    y, m = today.year, today.month - REFRESH_MONTHS
    while m < 1:
        m += 12
        y -= 1
    return f"{y}-{m:02d}"


def merge(old, new, cutoff):
    """Add new months; only overwrite months at or after cutoff."""
    changed = False
    for k, v in new.items():
        v = round(v, 3)
        if k not in old or (k >= cutoff and old[k] != v):
            old[k] = v
            changed = True
    return changed


def main():
    key, acc = os.environ["OCTOPUS_API_KEY"], os.environ["OCTOPUS_ACCOUNT"]
    auth = "Basic " + base64.b64encode((key + ":").encode()).decode()

    try:
        with open(OUT) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    data.setdefault("electricity", {})
    data.setdefault("gas", {})
    unit = os.environ.get("GAS_UNIT") or data.get("gas_unit", "m3")
    changed = data.get("gas_unit") != unit   # saving a changed unit counts as an update
    data["gas_unit"] = unit

    acct = get(f"{BASE}/accounts/{acc}/", auth)
    meters = find_meters(acct)
    cutoff = refresh_cutoff()
    fetched, errors = 0, 0

    for fuel, paths in meters.items():
        new = {}
        for path in paths:
            url = (f"{BASE}/{path}/consumption/?group_by=month&page_size=25000"
                   "&period_from=2015-01-01T00:00:00Z&order_by=period")
            try:
                for r in all_pages(url, auth):
                    k = month_key(r["interval_start"])
                    new[k] = new.get(k, 0) + r["consumption"]
                fetched += 1
            except Exception as e:  # keep going; other meters may work
                errors += 1
                print(f"warning: {fuel} meter failed: {e}", file=sys.stderr)
        changed |= merge(data[fuel], new, cutoff)

    if fetched == 0:
        print("No meter data fetched; leaving data.json untouched.", file=sys.stderr)
        sys.exit(1)

    if changed:
        data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data["electricity"] = dict(sorted(data["electricity"].items()))
        data["gas"] = dict(sorted(data["gas"].items()))
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w") as f:
            json.dump(data, f, indent=1)
        print("data.json updated.")
    else:
        print("No changes.")
    if errors:
        print(f"{errors} meter(s) failed; see warnings above.", file=sys.stderr)


if __name__ == "__main__":
    main()
