import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import collect

flt = collect.ScrapeFilter()
passed = failed = 0

POINTS = [
    ("Jersey City City Hall", "34", "017", 40.7178, -74.0431, True),
    ("Hoboken", "34", "017", 40.7440, -74.0324, True),
    ("Fort Lee", "34", "003", 40.8509, -73.9701, True),
    ("Carteret", "34", "023", 40.5773, -74.2282, True),
    ("Valley Stream", "36", "059", 40.6643, -73.7085, True),
    ("Great Neck Plaza", "36", "059", 40.7868, -73.7265, True),
    ("Manhattan, by county alone", "36", "061", 40.7580, -73.9855, True),
    ("Bayonne (partly outside the circles)", "34", "017", 40.6687, -74.1143, False),
    ("Kearny (partly outside the circles)", "34", "017", 40.7684, -74.1454, False),
    ("Elmont (unincorporated, between villages)", "36", "059", 40.7009, -73.7129, False),
    ("Yonkers (partly outside the circles)", "36", "119", 40.9312, -73.8988, False),
]
for name, st, co, lat, lon, want in POINTS:
    got = flt.keep({"stateFips": st, "countyFips": co, "latitude": lat, "longitude": lon})
    ok = got == want
    print(("PASS " if ok else "FAIL ") + "%s %s" % ("kept:" if want else "dropped:", name))
    passed, failed = passed + ok, failed + (not ok)

with open(os.path.join(os.path.dirname(HERE), "..", "cities", "nyc_zips.json")) as f:
    zips = json.load(f)["zips"]
zip_codes = {z["zip"] for z in zips}
for z in zips:
    xs, ys = [x for r in z["rings"] for x, _ in r], [y for r in z["rings"] for _, y in r]
    z["bbox"] = (min(xs), min(ys), max(xs), max(ys))


def zip_at(lat, lon):
    z = collect.city_areas.find(zips, lon, lat)
    return z and z["zip"]


areas = collect.city_areas.load(os.path.join(os.path.dirname(HERE), "..", "cities", "nyc_areas.json"))
CITY_ZIPS = ("100", "101", "102", "103", "104", "111", "112", "113", "114", "116", "11004", "11005")


def without_municipality():
    """Included ZIPs outside the five boroughs with under 1% of their area in an included municipality."""
    out = []
    for z in zips:
        if z["zip"].startswith(CITY_ZIPS):
            continue
        x0, y0, x1, y1 = z["bbox"]
        pts = [(x0 + (x1 - x0) * (i + 0.5) / 100, y0 + (y1 - y0) * (j + 0.5) / 100) for i in range(100) for j in range(100)]
        pts = [p for p in pts if collect.city_areas.find([z], *p)]
        if sum(1 for p in pts if collect.city_areas.find(areas, *p)) < 0.01 * len(pts):
            out.append(z["zip"])
    return out


ZIP_CHECKS = [("%s is in an included ZIP" % name, lambda lat=lat, lon=lon: zip_at(lat, lon) is not None)
              for name, st, co, lat, lon, want in POINTS if want]
ZIP_CHECKS += [
    ("Empire State Building is in 10001 or 10118", lambda: zip_at(40.7484, -73.9857) in ("10001", "10118")),
    ("Hoboken is in 07030", lambda: zip_at(40.7440, -74.0324) == "07030"),
    ("Bayonne's ZIP isn't included", lambda: "07002" not in zip_codes and zip_at(40.6687, -74.1143) is None),
    ("Kearny's ZIP isn't included", lambda: "07032" not in zip_codes and zip_at(40.7684, -74.1454) is None),
    ("Yonkers isn't in an included ZIP", lambda: zip_at(40.9312, -73.8988) is None),
    ("no Westchester ZIP is included", lambda: [z for z in zip_codes if z[:3] in ("105", "106", "107", "108")] == []),
    ("Mount Vernon isn't in an included ZIP", lambda: zip_at(40.9126, -73.8371) is None),
    ("Pelham isn't in an included ZIP", lambda: zip_at(40.9098, -73.8079) is None),
    ("ZIPs with only a few blocks of an included village aren't included", lambda: not zip_codes & {"11010", "11557", "11563"}),
    ("every ZIP outside the five boroughs has an included municipality in it", lambda: without_municipality() == []),
    ("a partly covered ZIP is whole: New Hyde Park's part of 11040 outside our areas", lambda: zip_at(40.7473, -73.6631) == "11040"
     and not flt.keep({"stateFips": "36", "countyFips": "059", "latitude": 40.7473, "longitude": -73.6631})),
    ("home counts match the housing mask's 4,000,922 to within 100", lambda: abs(sum(z["units"] for z in zips) - 4000922) < 100),
]
with open(os.path.join(os.path.dirname(HERE), "..", "cities", "nyc_streeteasy.json")) as f:
    hoods = json.load(f)["neighborhoods"]
PREFIXES = {"Manhattan": ("100", "101", "102"), "Bronx": ("104",), "Brooklyn": ("112",),
            "Queens": ("110", "111", "113", "114", "116")}


def misplaced():
    """Neighborhoods whose position isn't within about 150 m of their borough's ZIPs. Marble Hill is Manhattan's, in a Bronx ZIP."""
    steps = [(0, 0), (0.0015, 0), (-0.0015, 0), (0, 0.0015), (0, -0.0015)]
    return [h["name"] for h in hoods if h["name"] != "Marble Hill" and not any(
        (zip_at(h["pos"][0] + dy, h["pos"][1] + dx) or "").startswith(PREFIXES[h["borough"]]) for dy, dx in steps)]


def overlapping():
    return [(a["name"], b["name"]) for i, a in enumerate(hoods) for b in hoods[i + 1:]
            if abs(a["pos"][0] - b["pos"][0]) < 0.0025 and abs(a["pos"][1] - b["pos"][1]) < 0.0025]


ZIP_CHECKS += [
    ("176 StreetEasy neighborhoods have positions, each once", lambda: len(hoods) == len({(h["borough"], h["name"]) for h in hoods}) == 176),
    ("each position is in its own borough", lambda: misplaced() == []),
    ("no two positions are close enough to hide each other", lambda: overlapping() == []),
]
for name, test in ZIP_CHECKS:
    try:
        ok = bool(test())
    except Exception as e:
        ok, name = False, "%s (%s: %s)" % (name, type(e).__name__, e)
    print(("PASS " if ok else "FAIL ") + name)
    passed, failed = passed + ok, failed + (not ok)
print("area tests done: %d passed, %d failed" % (passed, failed))
