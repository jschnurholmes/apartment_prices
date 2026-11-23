"""Write cities/nyc_areas.json: places outside NYC wholly inside the RentCast search circles.

A municipality qualifies when its entire Census boundary, land and water, lies
inside the union of the circles in collect_config.json, so the monthly queries
return every listing in it. Candidates are county subdivisions (municipalities
in NJ; towns and cities in NY) and NY villages. Boundaries are simplified to
within SIMPLIFY_DEG; housing units come from the 2020 blocks whose interior
point falls in the area.

usage: python3 -I nyc/build_areas.py --cousub SHP... --villages SHP... --blocks SHP...
  TIGER/Line 2020 files from https://www2.census.gov/geo/tiger/TIGER2020/:
  COUSUB/tl_2020_{34,36}_cousub, PLACE/tl_2020_36_place,
  TABBLOCK20/tl_2020_{34,36}_tabblock20. Requires pyshp.
"""

import json
import math
import os
import sys

import shapefile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import city_areas

NYC_COUNTIES = {"36005", "36047", "36061", "36081", "36085"}
VILLAGE_LSAD = "47"
STEP_MI = 0.03
SIMPLIFY_DEG = 0.00003
EARTH_MI = 3958.8
OUT = os.path.join(ROOT, "cities", "nyc_areas.json")


def miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_MI * math.asin(math.sqrt(a))


def rings(shape):
    idx = list(shape.parts) + [len(shape.points)]
    return [[tuple(p) for p in shape.points[a:b]] for a, b in zip(idx, idx[1:])]


def wholly_inside(shape, circles):
    for ring in rings(shape):
        for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
            n = max(1, int(miles(y1, x1, y2, x2) / STEP_MI))
            for k in range(n):
                lat, lon = y1 + (y2 - y1) * k / n, x1 + (x2 - x1) * k / n
                if all(miles(lat, lon, c["latitude"], c["longitude"]) > c["radius"] for c in circles):
                    return False
    return True


def near(bbox, circles):
    x0, y0, x1, y1 = bbox
    return any(miles(c["latitude"], c["longitude"], min(max(c["latitude"], y0), y1),
                     min(max(c["longitude"], x0), x1)) <= c["radius"] for c in circles)


def simplify(points):
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        far, far_d = None, SIMPLIFY_DEG
        for i in range(a + 1, b):
            px, py = points[i]
            d = (abs(dy * px - dx * py + bx * ay - by * ax) / norm if norm
                 else math.hypot(px - ax, py - ay))
            if d > far_d:
                far, far_d = i, d
        if far is not None:
            keep[far] = True
            stack += [(a, far), (far, b)]
    out = [(round(x, 6), round(y, 6)) for (x, y), k in zip(points, keep) if k]
    return out if len(out) >= 4 else [(round(x, 6), round(y, 6)) for x, y in points]


def args(argv):
    groups, key = {"--cousub": [], "--villages": [], "--blocks": []}, None
    for a in argv:
        if a in groups:
            key = a
        elif key:
            groups[key].append(a)
        else:
            sys.exit(__doc__)
    return groups


def main(argv):
    files = args(argv)
    with open(os.path.join(HERE, "collect_config.json")) as f:
        circles = list(json.load(f)["circles"].values())
    areas = []
    for kind, paths in (("cousub", files["--cousub"]), ("village", files["--villages"])):
        for path in paths:
            for sr in shapefile.Reader(path).iterShapeRecords():
                r = sr.record.as_dict()
                if r["STATEFP"] + r.get("COUNTYFP", "") in NYC_COUNTIES:
                    continue
                if kind == "cousub" and r["COUSUBFP"] == "00000":
                    continue
                if kind == "village" and r["LSAD"] != VILLAGE_LSAD:
                    continue
                if not near(sr.shape.bbox, circles) or not wholly_inside(sr.shape, circles):
                    continue
                areas.append({"geoid": r["GEOID"], "name": r["NAME"],
                              "state": {"34": "NJ", "36": "NY"}[r["STATEFP"]],
                              "housing_units": 0,
                              "label": [round(float(r["INTPTLAT"]), 4), round(float(r["INTPTLON"]), 4)],
                              "rings": [simplify(ring) for ring in rings(sr.shape)]})
    for a in areas:
        xs = [x for ring in a["rings"] for x, _ in ring]
        ys = [y for ring in a["rings"] for _, y in ring]
        a["bbox"] = (min(xs), min(ys), max(xs), max(ys))
    for path in files["--blocks"]:
        sf = shapefile.Reader(path)
        names = [f[0] for f in sf.fields[1:]]
        ilat, ilon, ihu = names.index("INTPTLAT20"), names.index("INTPTLON20"), names.index("HOUSING20")
        for rec in sf.iterRecords():
            if rec[ihu] > 0:
                a = city_areas.find(areas, float(rec[ilon]), float(rec[ilat]))
                if a:
                    a["housing_units"] += rec[ihu]
    areas.sort(key=lambda a: (a["state"], -a["housing_units"]))
    for a in areas:
        del a["bbox"]
        print("%s %-22s %s %7d housing units" % (a["state"], a["name"], a["geoid"], a["housing_units"]))
    with open(OUT + ".tmp", "w") as f:
        json.dump({"areas": areas}, f, separators=(",", ":"))
    os.replace(OUT + ".tmp", OUT)
    print("wrote %s: %d areas, %d bytes" % (OUT, len(areas), os.path.getsize(OUT)))


if __name__ == "__main__":
    main(sys.argv[1:])
