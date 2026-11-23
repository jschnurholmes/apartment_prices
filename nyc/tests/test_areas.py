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


ZIP_CHECKS = [("%s is in an included ZIP" % name, lambda lat=lat, lon=lon: zip_at(lat, lon) is not None)
              for name, st, co, lat, lon, want in POINTS if want]
ZIP_CHECKS += [
    ("Empire State Building is in 10001 or 10118", lambda: zip_at(40.7484, -73.9857) in ("10001", "10118")),
    ("Hoboken is in 07030", lambda: zip_at(40.7440, -74.0324) == "07030"),
    ("Bayonne's ZIP isn't included", lambda: "07002" not in zip_codes and zip_at(40.6687, -74.1143) is None),
    ("Kearny's ZIP isn't included", lambda: "07032" not in zip_codes and zip_at(40.7684, -74.1454) is None),
    ("Yonkers isn't in an included ZIP", lambda: zip_at(40.9312, -73.8988) is None),
    ("a partly covered ZIP is whole: New Hyde Park's part of 11040 outside our areas", lambda: zip_at(40.7473, -73.6631) == "11040"
     and not flt.keep({"stateFips": "36", "countyFips": "059", "latitude": 40.7473, "longitude": -73.6631})),
    ("home counts match the housing mask's 4,000,922 to within 100", lambda: abs(sum(z["units"] for z in zips) - 4000922) < 100),
]
for name, test in ZIP_CHECKS:
    try:
        ok = bool(test())
    except Exception as e:
        ok, name = False, "%s (%s: %s)" % (name, type(e).__name__, e)
    print(("PASS " if ok else "FAIL ") + name)
    passed, failed = passed + ok, failed + (not ok)
print("area tests done: %d passed, %d failed" % (passed, failed))
