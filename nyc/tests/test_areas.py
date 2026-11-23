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
print("area tests done: %d passed, %d failed" % (passed, failed))
