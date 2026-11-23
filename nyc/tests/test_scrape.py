import gzip
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import collect


def listing(lid, br, price, lat=40.6900, lon=-73.9500, sqft=None, st="36", co="047"):
    r = {"id": lid, "bedrooms": br, "price": price, "latitude": lat, "longitude": lon,
         "stateFips": st, "countyFips": co}
    if sqft is not None:
        r["squareFootage"] = sqft
    return r


CASES = [
    (listing("room-sqft", 7, 1070, sqft=80, lon=-73.9401), False, "room: 7br at $1,070, 80 sqft"),
    (listing("room-1br", 1, 1654, sqft=120, lon=-73.9402), False, "room: 1br at $1,654, 120 sqft"),
    (listing("room-nosqft", 4, 1200, lon=-73.9403), False, "room: 4br at $1,200 ($300/br), no sqft"),
    (listing("building-9br", 9, 3500, sqft=3900, lon=-73.9404), True, "real: 9br building at $3,500, 3,900 sqft"),
    (listing("badsqft-3br", 3, 3000, sqft=5, lon=-73.9405), True, "real: 3br at $3,000 with a bogus 5 sqft"),
    (listing("studio", 0, 2300, sqft=289, lon=-73.9406), True, "real: studio at $2,300, 289 sqft"),
    (listing("big-nosqft", 5, 2500, lon=-73.9407), True, "real: 5br at $2,500 ($500/br), no sqft"),
    (listing("dup-a", 2, 3100, lon=-73.9408), True, "duplicate pair: first id kept"),
    (listing("dup-b", 2, 3100, lon=-73.9408), False, "duplicate pair: second id dropped"),
    (listing("same-spot-other-rent", 2, 3200, lon=-73.9408), True, "same spot, different rent: kept"),
    (listing("jersey-city", 1, 2600, lat=40.7178, lon=-74.0431, st="34", co="017"), True, "Jersey City (in the areas): kept"),
    (listing("bayonne", 1, 2100, lat=40.6687, lon=-74.1143, st="34", co="017"), False, "Bayonne (not in the areas): dropped"),
]

passed = failed = 0
with tempfile.TemporaryDirectory() as d:
    os.makedirs(os.path.join(d, "raw"))
    with gzip.open(os.path.join(d, "raw", "p-00000.json.gz"), "wt") as f:
        json.dump([c[0] for c in CASES], f)
    led = {"period_start": "2026-11-21", "scrape_ts": 1795000000,
           "calls": [{"state": "done", "status": 200, "file": "raw/p-00000.json.gz"}]}
    collect.write_scrape(d, led, collect.ScrapeFilter())
    with open(collect.scrape_path(d, led)) as f:
        ids = {line.split()[2] for line in f if line[:1].isdigit()}
for r, want, name in CASES:
    ok = (r["id"] in ids) == want
    print(("PASS " if ok else "FAIL ") + name)
    passed, failed = passed + ok, failed + (not ok)
print("scrape tests done: %d passed, %d failed" % (passed, failed))
