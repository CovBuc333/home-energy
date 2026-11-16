#!/usr/bin/env python3
"""Import older usage from an Octopus half-hourly export (Energy_Usage.xlsx)
into site/data.json, so the site can compare years before the API's history.

Run locally (needs openpyxl):  python tools/import_history.py Energy_Usage.xlsx

What it does
- Totals the half-hourly readings in every "... Raw" sheet by month and
  works out coverage (readings received / readings expected).
- Months the API already provides are kept as they are, except the API's first
  month, which is usually partial (account start) - the export wins there if
  it has better coverage.
- Months with no raw readings use the Overview sheet's typed-in figure if there
  is one (or if it is clearly fuller than the raw readings).
- Gaps are estimated, kept separate from real readings, and flagged:
    coverage >= 96.5%        -> treated as a complete month
    25% <= coverage < 96.5%  -> estimate = readings scaled up to a full month
    coverage < 25% / missing -> estimate = that month's usual share of a year,
                                scaled to the rest of that year's usage
  Written to data.json as:
    quality[fuel][month]   = % of readings present (only for gap months)
    estimates[fuel][month] = estimated full-month value
    notes                  = plain-English list of what was filled and how
- The raw export itself is NOT written anywhere (half-hourly data reveals when
  the house is occupied); only monthly totals end up in the repo.
"""
import calendar
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "site", "data.json")
FULL, SCALE_MIN = 0.965, 0.25
FUELS = ("electricity", "gas")
GAS_KWH_PER_M3 = 11.2
MON = calendar.month_abbr


def month_key(y, m):
    return f"{y}-{m:02d}"


def label(k):
    y, m = k.split("-")
    return f"{MON[int(m)]} {y}"


def read_raw(wb, gas_unit):
    """{fuel: {month: [total, readings, expected]}} using the best sheet per month."""
    best = {f: {} for f in FUELS}
    for ws in wb.worksheets:
        if "raw" not in ws.title.lower():
            continue
        fuel = "gas" if "gas" in ws.title.lower() else "electricity"
        rows = ws.iter_rows(values_only=True)
        head = str(next(rows)[0] or "").lower()
        to_kwh = fuel == "gas" and "kwh" in head and "m" not in head.replace("kwh", "")
        tot, n = defaultdict(float), defaultdict(int)
        for r in rows:
            v = r[0]
            d = next((x for x in r if isinstance(x, datetime)), None)
            if d is None or v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if to_kwh and gas_unit == "m3":
                v /= GAS_KWH_PER_M3
            k = month_key(d.year, d.month)
            tot[k] += v
            n[k] += 1
        for k in tot:
            y, m = map(int, k.split("-"))
            exp = calendar.monthrange(y, m)[1] * 48
            if k not in best[fuel] or n[k] > best[fuel][k][1]:
                best[fuel][k] = [tot[k], n[k], exp]
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
    wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
    raw, ov = read_raw(wb, gas_unit), read_overview(wb)

    quality = {f: {} for f in FUELS}
    estimates = {f: {} for f in FUELS}
    notes = []

    for fuel in FUELS:
        api = data[fuel]
        first_api = min(api) if api else "9999-99"
        merged, cov, from_sheet = {}, {}, []
        months = sorted(set(raw[fuel]) | set(ov[fuel]))
        for k in months:
            if k in api and k != first_api:
                continue  # API is authoritative for its complete months
            t, n, exp = raw[fuel].get(k, [0.0, 0, 1])
            c = n / exp
            if k in api and k == first_api and c <= 0.5:
                continue  # export no better than the API's partial first month
            o = ov[fuel].get(k)
            if o is not None and (n == 0 or (c < 0.9 and o > t * 1.2)):
                merged[k], cov[k] = round(o, 3), 1.0
                from_sheet.append(k)
                continue
            if n:
                merged[k], cov[k] = round(t, 3), c

        # fill missing months between first and last month of history
        all_keys = sorted(set(merged) | set(api))
        y0, m0 = map(int, all_keys[0].split("-"))
        last_full = max(k for k in api) if api else all_keys[-1]
        y, m = y0, 1  # from January of the first year so every year is comparable
        while month_key(y, m) < last_full:
            k = month_key(y, m)
            if k not in merged and k not in api:
                cov[k] = 0.0
            m += 1
            if m == 13:
                y, m = y + 1, 1

        values = {**{k: v for k, v in api.items()}, **merged}
        gap = {k: c for k, c in cov.items() if c < FULL}

        # first pass: scale up partial months with enough readings
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
            if not known or not ref:
                continue
            level = sum(usable[month_key(y, m)] for m in known) / sum(share[m] for m in known)
            est[k] = share[int(k[5:])] * level

        data[fuel] = dict(sorted(values.items()))
        for k in sorted(gap):
            if k in est:
                quality[fuel][k] = round(gap[k] * 100)
                estimates[fuel][k] = round(est[k], 3)

        name = "Electricity" if fuel == "electricity" else "Gas"
        sc = [k for k in sorted(estimates[fuel]) if gap[k] >= SCALE_MIN]
        se = [k for k in sorted(estimates[fuel]) if gap[k] < SCALE_MIN]
        if sc:
            notes.append(f"{name} – scaled up from partial readings: "
                         + ", ".join(f"{label(k)} ({quality[fuel][k]}%)" for k in sc) + ".")
        if se:
            notes.append(f"{name} – estimated from the usual seasonal pattern (little or no readings): "
                         + ", ".join(label(k) for k in se) + ".")
        if from_sheet:
            notes.append(f"{name} – taken from the Overview sheet (no half-hourly readings to check against): "
                         + ", ".join(label(k) for k in from_sheet) + ".")
        if ref:
            notes.append(f"{name} seasonal pattern based on {', '.join(map(str, ref))}.")

    data["quality"], data["estimates"] = quality, estimates
    data["notes"] = ["Usage before November 2024 is imported from an earlier Octopus export; "
                     "later months come from the Octopus API."] + notes
    with open(OUT, "w") as f:
        json.dump(data, f, indent=1)
    print("\n".join(data["notes"]))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python tools/import_history.py Energy_Usage.xlsx")
    main(sys.argv[1])
