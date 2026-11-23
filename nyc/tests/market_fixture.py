"""Write synthetic Zillow and StreetEasy files in the published layout.

usage: python3 -I market_fixture.py DIR LAST_MONTH
"""

import csv
import io
import os
import sys
import zipfile

SIZES = ("Studio", "OneBd", "TwoBd", "ThreePlusBd")
BOROUGHS = ("Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island")
N_HOODS = 140


def months(first, last):
    y, m = int(first[:4]), int(first[5:])
    out = []
    while "%04d-%02d" % (y, m) <= last:
        out.append("%04d-%02d" % (y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def to_csv(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode()


def zipped(name, data):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def areas():
    yield "NYC", "", "city"
    for b in BOROUGHS:
        yield b, b, "borough"
    for i in range(N_HOODS):
        yield "Hood %03d" % i, BOROUGHS[i % 4], "neighborhood"


def streeteasy(last, hook=None):
    """{relative path: bytes}. hook(metric, size, area, month, value) may replace a cell."""
    out = {}
    for si, size in enumerate(SIZES):
        for metric in ("medianAskingRent", "rentalInventory"):
            ms = months("2010-01", last)
            rows = [["areaName", "Borough", "areaType"] + ms]
            for k, (name, boro, kind) in enumerate(areas()):
                cells = [2000 + 500 * si + 10 * k + t if metric == "medianAskingRent" else 20 for t in range(len(ms))]
                if hook:
                    cells = [hook(metric, size, name, m, v) for m, v in zip(ms, cells)]
                rows.append([name, boro, kind] + cells)
            base = "%s_%s" % (metric, size)
            out["%s/%s.zip" % (size, base)] = zipped(base + ".csv", to_csv(rows))
    rows = [["month", "Brooklyn", "Manhattan", "NYC", "Queens"]]
    for t, m in enumerate(months("2007-01", last)):
        rows.append([m, "" if t < 36 else 2000 + t, 2500 + t, 2200 + t, "" if t < 36 else 1800 + t])
    out["All/rentalIndex_All.zip"] = zipped("rentalIndex_All.csv", to_csv(rows))
    return out


ZIPS = [("10001", "NY", "New York County"), ("11201", "NY", "Kings County"), ("11101", "NY", "Queens County"),
        ("10451", "NY", "Bronx County"), ("10301", "NY", "Richmond County"), ("11040", "NY", "Nassau County"),
        ("07030", "NJ", "Hudson County"), ("07002", "NJ", "Hudson County"), ("90210", "CA", "Los Angeles County")]


def zillow(last, hook=None):
    ms = months("2015-01", last)
    rows = [["RegionID", "SizeRank", "RegionName", "RegionType", "StateName", "State", "City", "Metro", "CountyName"]
            + [m + "-28" for m in ms]]
    extra = [("%05d" % (99000 + i), "NY", "Kings County") for i in range(110)]
    for k, (z, st, county) in enumerate(ZIPS + extra):
        cells = [2000 + 100 * (k % 9) + t + 0.25 for t in range(len(ms))]
        if hook:
            cells = [hook(z, m, v) for m, v in zip(ms, cells)]
        rows.append([k, k, z.lstrip("0"), "zip", st, st, "City", "Metro", county] + cells)
    return {"zip_rents.csv": to_csv(rows)}


def write(directory, last):
    files = {"streeteasy/" + k: v for k, v in streeteasy(last).items()}
    files.update({"zillow/" + k: v for k, v in zillow(last).items()})
    for rel, data in files.items():
        path = os.path.join(directory, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)


if __name__ == "__main__":
    write(sys.argv[1], sys.argv[2])
