#!/usr/bin/env python3
"""Import older usage from an Octopus half-hourly export (Energy_Usage.xlsx)
into site/data.json, so the site can compare years before the API's history.

Run locally (needs openpyxl):  python tools/import_history.py Energy_Usage.xlsx
Safe to re-run: it rebuilds the imported history each time.

What it does
- Reads every "... Raw" sheet, keeps the best sheet's readings for each day
  (sheets overlap at year ends) and totals them by month and by week.
- Drops any date ranges listed in EXCLUDE (readings that look wrong). Excluded
  months are left blank and never estimated.
- Months the API provides (from api_start) are kept, except the API's first
  month, which is usually partial - the export wins there if it is fuller.
- Months with no raw readings use the Overview sheet's typed-in figure if there
  is one (or if it is clearly fuller than the raw readings).
- Monthly gaps are estimated, kept separate from real readings, and flagged:
    coverage >= 96.5%        -> treated as a complete month
    25% <= coverage < 96.5%  -> estimate = readings scaled up to a full month
    coverage < 25% / missing -> estimate = that month's usual share of a year,
                                scaled to the rest of that year's usage
- Weekly totals (Monday-start weeks) and daily totals are imported for complete
  weeks/days before the API's weekly/daily data begins; incomplete ones are
  left out, not estimated.
- The raw export itself is NOT written anywhere (half-hourly data reveals when
  the house is occupied); only monthly, weekly and daily totals end up in the repo.
"""
import calendar
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "site", "data.json")
FULL, SCALE_MIN = 0.965, 0.25
FUELS = ("electricity", "gas")
GAS_KWH_PER_M3 = 11.2
MON = calendar.month_abbr
NAME = {"electricity": "Electricity", "gas": "Gas"}

# Readings to ignore, as inclusive (first day, last day) ranges per fuel.
EXCLUDE = {
    "electricity": [("2021-01-01", "2021-05-31", "readings looked wrong")],
    "gas": [],
}
# First Monday whose weekly total comes from the API rather than the export.
WEEKLY_API_FROM = "2024-12-02"


def month_key(y, m):
    return f"{y}-{m:02d}"


def label(k):
    y, m = k.split("-")
    return f"{MON[int(m)]} {y}"


def excluded(fuel, d):
    s = d.isoformat()
    return any(a <= s <= b for a, b, _ in EXCLUDE[fuel])


def excluded_month(fuel, k):
    y, m = map(int, k.split("-"))
    days = calendar.monthrange(y, m)[1]
    return all(excluded(fuel, date(y, m, d)) for d in range(1, days + 1))


def read_days(wb, gas_unit):
    """{fuel: {date: [total, readings]}}, best sheet per day, excluded days dropped."""
    best = {f: {} for f in FUELS}
    for ws in wb.worksheets:
        if "raw" not in ws.title.lower():
            continue
        fuel = "gas" if "gas" in ws.title.lower() else "electricity"
        rows = ws.iter_rows(values_only=True)
        head = str(next(rows)[0] or "").lower()
        to_m3 = fuel == "gas" and "kwh" in head and gas_unit == "m3"
        day = defaultdict(lambda: [0.0, 0])
        for r in rows:
            v = r[0]
            d = next((x for x in r if isinstance(x, datetime)), None)
            if d is None or v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            day[d.date()][0] += v / GAS_KWH_PER_M3 if to_m3 else v
            day[d.date()][1] += 1
        for d, (t, n) in day.items():
            if excluded(fuel, d):
                continue
            if d not in best[fuel] or n > best[fuel][d][1]:
                best[fuel][d] = [t, n]
    return best


def read_overview(wb):
    """Typed-in monthly figures from the Overview sheet: {fuel: {month: value}}."""
    out = {f: {} for f in FUELS}
    if "Overview" not in wb.sheetnames:
        return out
    fuel = None
    for r in wb["Overview"].iter_rows(values_only=True):
        if not r:
            continue
        a = r[0]
        if isinstance(a, str) and a.strip().lower().startswith("elec"):
            fuel = "electricity"
        elif isinstance(a, str) and a.strip().lower().startswith("gas"):
            fuel = "gas"
        elif fuel and isinstance(a, (int, float)) and 2000 < a < 2100:
            for m in range(1, 13):
                v = r[m] if m < len(r) else None
                if isinstance(v, (int, float)):
                    out[fuel][month_key(int(a), m)] = float(v)
    return out


def main(xlsx):
    with open(OUT) as f:
        data = json.load(f)
    gas_unit = data.get("gas_unit", "m3")
    api_start = data.get("api_start") or {f: min(data[f]) for f in FUELS if data[f]}
    wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
    days, ov = read_days(wb, gas_unit), read_overview(wb)

    quality = {f: {} for f in FUELS}
    estimates = {f: {} for f in FUELS}
    weekly = data.get("weekly") or {f: {} for f in FUELS}
    daily = data.get("daily") or {f: {} for f in FUELS}
    notes = []

    for fuel in FUELS:
        start = api_start[fuel]
        api = {k: v for k, v in data[fuel].items() if k >= start}

        # monthly totals and coverage from the de-duplicated daily readings
        mt, mn = defaultdict(float), defaultdict(int)
        for d, (t, n) in days[fuel].items():
            k = month_key(d.year, d.month)
            mt[k] += t
            mn[k] += n
        raw = {k: (mt[k], mn[k] / (calendar.monthrange(*map(int, k.split("-")))[1] * 48)) for k in mt}

        merged, cov, from_sheet = {}, {}, []
        for k in sorted(set(raw) | set(ov[fuel])):
            if excluded_month(fuel, k) or (k in api and k != start):
                continue
            t, c = raw.get(k, (0.0, 0.0))
            if k == start and c <= 0.5:
                continue  # export no better than the API's partial first month
            o = ov[fuel].get(k)
            if o is not None and (c == 0 or (c < 0.9 and o > t * 1.2)):
                merged[k], cov[k] = round(o, 3), 1.0
                from_sheet.append(k)
            elif c:
                merged[k], cov[k] = round(t, 3), c

        # missing months from January of the first year up to the API's data
        first = min(set(merged) | set(api))
        y, m = int(first[:4]), 1
        while month_key(y, m) < start:
            k = month_key(y, m)
            if k not in merged and not excluded_month(fuel, k):
                cov[k] = 0.0
            m += 1
            if m == 13:
                y, m = y + 1, 1

        values = {**api, **merged}
        gap = {k: c for k, c in cov.items() if c < FULL}
        scaled = {k: values[k] / c for k, c in gap.items() if c >= SCALE_MIN}
        usable = {k: v for k, v in values.items() if k not in gap}
        usable.update(scaled)

        # seasonal profile from years where all 12 months are usable
        years = sorted({int(k[:4]) for k in usable})
        ref = [y for y in years if all(month_key(y, m) in usable for m in range(1, 13))]
        share = [0.0] * 13
        for y in ref:
            tot = sum(usable[month_key(y, m)] for m in range(1, 13))
            for m in range(1, 13):
                share[m] += usable[month_key(y, m)] / tot / len(ref)

        est = dict(scaled)
        for k, c in gap.items():
            if c >= SCALE_MIN:
                continue
            y = int(k[:4])
            known = [m for m in range(1, 13) if month_key(y, m) in usable]
            if known and ref:
                level = sum(usable[month_key(y, m)] for m in known) / sum(share[m] for m in known)
                est[k] = share[int(k[5:])] * level

        data[fuel] = dict(sorted(values.items()))
        for k in sorted(gap):
            if k in est:
                quality[fuel][k] = round(gap[k] * 100)
                estimates[fuel][k] = round(est[k], 3)

        # weekly totals: complete Monday-start weeks before the API's weekly data
        wk = {k: v for k, v in weekly.get(fuel, {}).items() if k >= WEEKLY_API_FROM}
        mondays = sorted({d - timedelta(days=d.weekday()) for d in days[fuel]})
        for mon in mondays:
            if mon.isoformat() >= WEEKLY_API_FROM:
                continue
            ds = [days[fuel].get(mon + timedelta(days=i)) for i in range(7)]
            if all(x and x[1] >= 46 for x in ds):
                wk[mon.isoformat()] = round(sum(x[0] for x in ds), 3)
        weekly[fuel] = dict(sorted(wk.items()))

        # daily totals: complete days before the API's daily data (same start as weekly)
        dy = {k: v for k, v in daily.get(fuel, {}).items() if k >= WEEKLY_API_FROM}
        for d, (t, n) in days[fuel].items():
            if n >= 46 and d.isoformat() < WEEKLY_API_FROM:
                dy[d.isoformat()] = round(t, 2)
        daily[fuel] = dict(sorted(dy.items()))

        n = NAME[fuel]
        for a, b, why in EXCLUDE[fuel]:
            notes.append(f"{n} – {date.fromisoformat(a):%-d %b %Y} to {date.fromisoformat(b):%-d %b %Y} "
                         f"removed ({why}); those months are left blank.")
        sc = [k for k in sorted(estimates[fuel]) if gap[k] >= SCALE_MIN]
        se = [k for k in sorted(estimates[fuel]) if gap[k] < SCALE_MIN]
        if sc:
            notes.append(f"{n} – scaled up from partial readings: "
                         + ", ".join(f"{label(k)} ({quality[fuel][k]}%)" for k in sc) + ".")
        if se:
            notes.append(f"{n} – estimated from the usual seasonal pattern (little or no readings): "
                         + ", ".join(label(k) for k in se) + ".")
        if from_sheet:
            notes.append(f"{n} – taken from the Overview sheet (no half-hourly readings to check against): "
                         + ", ".join(label(k) for k in from_sheet) + ".")
        if ref:
            notes.append(f"{n} seasonal pattern based on {', '.join(map(str, ref))}.")

    data["api_start"] = api_start
    data["weekly_api_from"] = WEEKLY_API_FROM
    data["quality"], data["estimates"], data["weekly"], data["daily"] = quality, estimates, weekly, daily
    data["notes"] = ["Usage before November 2024 is imported from an earlier Octopus export; "
                     "later months come from the Octopus API. Weekly and daily figures only include complete weeks and days."] + notes
    with open(OUT, "w") as f:
        json.dump(data, f, indent=1)
    print("\n".join(data["notes"]))
    for fuel in FUELS:
        print(fuel, "weeks:", len(weekly[fuel]), "days:", len(daily[fuel]), min(daily[fuel], default=None), "->", max(daily[fuel], default=None))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python tools/import_history.py Energy_Usage.xlsx")
    main(sys.argv[1])
