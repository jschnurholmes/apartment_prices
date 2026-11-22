"""Compare RentCast listing rents with StreetEasy median asking rents.

Each RentCast listing is placed in a 2020 NTA, mapped to the StreetEasy
neighborhood(s) with matching names, and divided by StreetEasy's median for
that neighborhood and bedroom size.  The median of these ratios, by borough
and by size, shows whether RentCast's listings skew cheaper or pricier than
StreetEasy's.

Usage: python3 -I benchmark_streeteasy.py [YYYY-MM]   (default: latest month)
"""

import csv
import glob
import json
import os
import re
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
NYC = {"36061": "Manhattan", "36047": "Brooklyn", "36081": "Queens",
       "36005": "Bronx", "36085": "Staten Island"}
SIZES = {"Studio": 0, "OneBd": 1, "TwoBd": 2, "ThreePlusBd": 3}
SIZE_NAMES = {0: "studio", 1: "1BR", 2: "2BR", 3: "3BR+"}


def norm(name):
    name = re.sub(r"\(.*?\)", "", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def load_streeteasy(month):
    """{(borough, norm_area): {size: median}} and the month used."""
    medians = defaultdict(dict)
    for path in glob.glob(os.path.join(HERE, "data", "streeteasy", "csv", "medianAskingRent_*.csv")):
        key = os.path.basename(path)[len("medianAskingRent_"):-len(".csv")]
        if key not in SIZES:
            continue
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        header = rows[0]
        month = month or header[-1]
        col = header.index(month)
        for r in rows[1:]:
            area, borough, kind = r[0], r[1], r[2]
            value = r[col] or r[col - 1]
            if value:
                medians[(borough, norm(area), kind)][SIZES[key]] = float(value)
    return medians, month


def point_in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def load_ntas():
    with open(os.path.join(HERE, "data", "nyc", "nta2020.geojson")) as f:
        features = json.load(f)["features"]
    ntas = []
    for feat in features:
        p = feat["properties"]
        polys = feat["geometry"]["coordinates"]
        xs = [pt[0] for poly in polys for pt in poly[0]]
        ys = [pt[1] for poly in polys for pt in poly[0]]
        ntas.append({"name": p["ntaname"], "boro": p["boroname"], "polys": polys,
                     "bbox": (min(xs), min(ys), max(xs), max(ys))})
    return ntas


def find_nta(ntas, lon, lat):
    for n in ntas:
        x0, y0, x1, y1 = n["bbox"]
        if not (x0 <= lon <= x1 and y0 <= lat <= y1):
            continue
        for poly in n["polys"]:
            if point_in_ring(lon, lat, poly[0]) and not any(
                    point_in_ring(lon, lat, hole) for hole in poly[1:]):
                return n
    return None


ALIASES = {
    "Gramercy": ["Gramercy Park"],
    "Stuyvesant Town-Peter Cooper Village": ["Stuyvesant Town/PCV"],
    "East Midtown-Turtle Bay": ["Midtown East"],
    "Old Astoria-Hallets Point": ["Astoria"],
    "Queensbridge-Ravenswood-Dutch Kills": ["Long Island City"],
}


def se_areas_for(nta, medians):
    parts = [norm(nta["name"])] + [norm(s) for s in nta["name"].split("-")]
    parts += [norm(a) for a in ALIASES.get(nta["name"], [])]
    found = []
    for part in parts:
        key = (nta["boro"], part, "neighborhood")
        if key in medians and key not in found:
            found.append(key)
    return found


def load_listings():
    listings = {}
    for path in sorted(glob.glob(os.path.join(HERE, "data", "raw", "call-*.json"))):
        with open(path) as f:
            for r in json.load(f):
                if r.get("stateFips", "") + r.get("countyFips", "") in NYC and r.get("price"):
                    listings[r["id"]] = r
    return list(listings.values())


def main():
    medians, month = load_streeteasy(sys.argv[1] if len(sys.argv) > 1 else None)
    ntas = load_ntas()
    listings = load_listings()
    print("StreetEasy month: %s | RentCast NYC listings: %d" % (month, len(listings)))

    ratios = defaultdict(list)
    unmatched = defaultdict(int)
    for r in listings:
        boro = NYC[r["stateFips"] + r["countyFips"]]
        size = min(r.get("bedrooms") or 0, 3)
        nta = find_nta(ntas, r["longitude"], r["latitude"])
        areas = se_areas_for(nta, medians) if nta else []
        values = [medians[a][size] for a in areas if size in medians[a]]
        if not values:
            borough_median = medians.get((boro, norm(boro), "borough"), {}).get(size)
            if boro == "Staten Island" and borough_median:
                ratios[("fallback", boro)].append(r["price"] / borough_median)
            unmatched[nta["name"] if nta else "(outside NTAs)"] += 1
            continue
        ratio = r["price"] / statistics.median(values)
        ratios[(boro, size)].append(ratio)
        ratios[(boro, None)].append(ratio)
        ratios[(None, size)].append(ratio)
        ratios[(None, None)].append(ratio)

    matched = len(ratios[(None, None)])
    print("matched to a StreetEasy neighborhood median: %d of %d" % (matched, len(listings)))
    print("\nmedian(RentCast rent / StreetEasy median), n in parentheses")
    print("%-14s" % "" + "".join("%-14s" % SIZE_NAMES[s] for s in range(4)) + "all sizes")
    for boro in ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island", None]:
        cells = []
        for size in [0, 1, 2, 3, None]:
            v = ratios.get((boro, size), [])
            cells.append("%.2f (%d)" % (statistics.median(v), len(v)) if v else "-")
        print("%-14s" % (boro or "All NYC") + "".join("%-14s" % c for c in cells))

    si = ratios.get(("fallback", "Staten Island"), [])
    if si:
        print("\nStaten Island vs StreetEasy borough median (no neighborhood data): %.2f (%d)"
              % (statistics.median(si), len(si)))

    print("\nlargest unmatched NTAs:")
    for name, n in sorted(unmatched.items(), key=lambda t: -t[1])[:8]:
        print("  %3d  %s" % (n, name))


if __name__ == "__main__":
    main()
