import io
import json
import lzma
import os
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import build_market
import collect
import market_data as md
import market_fixture as fx

passed = failed = 0


def check(name, test):
    global passed, failed
    try:
        ok = test()
    except Exception as e:
        ok, name = False, "%s (%s: %s)" % (name, type(e).__name__, e)
    print(("PASS " if ok else "FAIL ") + name)
    passed, failed = passed + bool(ok), failed + (not ok)


def flat(files):
    return {os.path.basename(k): v for k, v in files.items()}


def rejects(source, new, old, words):
    try:
        md.check(source, new, old)
    except md.MarketDataError as e:
        return words in str(e)
    return False


def loads(d, source):
    return md.stored(d, source)["months"][-1]


SE, ZI = flat(fx.streeteasy("2026-10")), fx.zillow("2026-10")


def se(last="2026-10", **kw):
    return flat(fx.streeteasy(last, **kw))


def without(files, name):
    return {k: v for k, v in files.items() if k != name}


p = md.parse("streeteasy", SE)
check("StreetEasy parses: 146 areas, 202 months, 4 sizes", lambda: len(p["areas"]) == 146 and len(p["months"]) == 202
      and all(set(a["rent"]) == set(a["n"]) == {0, 1, 2, 3} for a in p["areas"]))
check("StreetEasy index: blank early months are None", lambda: p["index"]["series"]["Brooklyn"][0] is None
      and p["index"]["series"]["NYC"][0] == 2200)
zips = md.parse("zillow", ZI)["zips"]
check("Zillow parses: NY and NJ only, leading zeros restored", lambda: "07030" in zips and "90210" not in zips)
check("Zillow marks only the five boroughs as NYC", lambda: zips["10301"]["nyc"] and not zips["11040"]["nyc"]
      and not zips["07030"]["nyc"])
for source, files in (("streeteasy", SE), ("zillow", ZI)):
    check("good %s files pass, alone and beside themselves" % source,
          lambda: not rejects(source, files, None, "") and not rejects(source, files, files, ""))

bad = {
    "Zillow: too few NYC ZIPs": ("zillow", fx.zillow("2026-10", hook=lambda z, m, v: "" if z > "99020" and m == "2026-10" else v), None, "NYC ZIPs"),
    "Zillow: a month missing": ("zillow", {"zip_rents.csv": ZI["zip_rents.csv"].replace(b"2020-05-28", b"2020-05-xx")}, None, "consecutive"),
    "Zillow: a renamed column": ("zillow", {"zip_rents.csv": ZI["zip_rents.csv"].replace(b"CountyName", b"County")}, None, "missing columns"),
    "Zillow: an HTML error page": ("zillow", {"zip_rents.csv": b"<html>Access denied</html>"}, None, "missing columns"),
    "Zillow: a truncated file": ("zillow", {"zip_rents.csv": ZI["zip_rents.csv"][:-30]}, None, "row's length"),
    "Zillow: a non-numeric rent": ("zillow", fx.zillow("2026-10", hook=lambda z, m, v: "n/a" if z == "10001" and m == "2020-05" else v), None, "not a number"),
    "Zillow: ends before the stored copy": ("zillow", fx.zillow("2026-09"), ZI, "before the stored"),
    "StreetEasy: ends before the stored copy": ("streeteasy", se("2026-09"), SE, "before the stored"),
    "StreetEasy: a truncated zip": ("streeteasy", dict(SE, **{"medianAskingRent_TwoBd.zip": SE["medianAskingRent_TwoBd.zip"][:500]}), None, "unreadable zip"),
    "StreetEasy: damaged bytes inside a zip": ("streeteasy", dict(SE, **{"medianAskingRent_TwoBd.zip": SE["medianAskingRent_TwoBd.zip"][:300] + b"\0" * 60 + SE["medianAskingRent_TwoBd.zip"][360:]}), None, "unreadable zip"),
    "StreetEasy: one file a month ahead": ("streeteasy", dict(SE, **{"rentalInventory_OneBd.zip": se("2026-11")["rentalInventory_OneBd.zip"]}), None, "months differ"),
    "StreetEasy: too few neighborhoods this month": ("streeteasy", se(hook=lambda f, s, a, m, v: "" if f == "medianAskingRent" and m == "2026-10" and a > "Hood 050" and a < "Hood 999" else v), None, "neighborhoods"),
    "StreetEasy: a negative rent": ("streeteasy", se(hook=lambda f, s, a, m, v: -5 if a == "Hood 003" and m == "2020-05" else v), None, "out of range"),
}
for name, (source, new, old, words) in bad.items():
    check("rejected: " + name, lambda: rejects(source, new, old, words))
fewer = se(hook=lambda f, s, a, m, v: "" if f == "medianAskingRent" and m == "2026-10" and "Hood 105" <= a else v)
check("StreetEasy: 105 of 140 neighborhoods is accepted alone", lambda: not rejects("streeteasy", fewer, None, ""))
check("StreetEasy: 105 of 140 neighborhoods is rejected beside a full stored copy",
      lambda: rejects("streeteasy", fewer, SE, "far fewer"))

check("pooling: 5 listings is enough for the month's own median", lambda: md.pooled([1000, 2000], [9, 5]) == [(1000, False), (2000, False)])
check("pooling: 4 listings averages 3 months by listing count", lambda: md.pooled([9000, 1000, 2000, 4000], [50, 10, 6, 4])[3]
      == ((1000 * 10 + 2000 * 6 + 4000 * 4) / 20, True))
check("pooling: a month with no listings uses the two before it", lambda: md.pooled([1000, 2000, None], [1, 3, 0])[2] == (1750, True))
check("pooling: nothing in 3 months is no value", lambda: md.pooled([1000, None, None, None], [9, 0, 0, 0])[3] == (None, False))
check("pooling: the first month has nothing earlier to use", lambda: md.pooled([1500], [2]) == [(1500, True)])


def built():
    data = {"months": ["2026-09", "2026-10"], "index": {"months": ["2026-10"], "series": {"NYC": [4014.0], "Queens": [None]}},
            "areas": [{"name": "X", "borough": "Queens", "kind": "neighborhood", "rent": {0: [1994.0, 2026.0], 1: [None, 3000.0]},
                       "n": {0: [7.0, 2.0], 1: [None, 0.0]}}]}
    return build_market.streeteasy(data, {"2026-08": 330.0, "2026-09": 331.0})


check("site data: $10 steps, thin rents negative, counts whole", lambda: built()["areas"][0]["rent"] == {"0": [1990, -2000], "1": [None, None]}
      and built()["areas"][0]["n"] == {"0": [7, 2], "1": [0, 0]})
check("site data: the latest CPI is carried forward", lambda: built()["cpi"] == [331.0, 331.0] and built()["cpi_latest_month"] == "2026-09")
check("site data: index in $10 steps, gaps kept", lambda: built()["index"]["series"] == {"NYC": [4010], "Queens": [None]})


def zillow_built():
    rent = {"10001": [4000.0, 4004.0], "10002": [3000.0, None], "07030": [None, 5000.0], "07002": [1.0, 1.0], "10003": [None, None]}
    return build_market.zillow({"months": ["2026-09", "2026-10"], "zips": {z: {"nyc": True, "rent": r} for z, r in rent.items()}},
                               {"2026-09": 331.0})


def weighted():
    units = {z["zip"]: z["units"] for z in json.load(open(build_market.ZIPS))["zips"]}
    return units["10002"] > units["10001"] and units["07030"] > units["10001"]


check("Zillow site data: only the map's ZIPs that have a rent, in $10 steps", lambda: {z["zip"]: z["rent"] for z in zillow_built()["zips"]}
      == {"10001": [4000, 4000], "10002": [3000, None], "07030": [None, 5000]})
check("Zillow site data: every ZIP has its outline", lambda: all(len(z["rings"][0]) >= 4 for z in zillow_built()["zips"]))
check("Zillow site data: the citywide median is weighted by homes", lambda: weighted() and zillow_built()["city"] == [3000, 5000])


def archived():
    led = {"period_start": "2026-11-21", "calls": [], "market_data": "0123456789abcdef", "scrape_ts": 1}
    with tempfile.TemporaryDirectory() as d:
        tar = tarfile.open(fileobj=io.BytesIO(lzma.decompress(collect.backup_archive(d, led))))
        return json.load(tar.extractfile("runs/2026-11-21.json"))


check("backups leave out the market data mark, so a restore downloads again", lambda: archived() == {"period_start": "2026-11-21", "calls": [], "scrape_ts": 1})

BASE = "http://files.example.test"
os.environ[md.ZILLOW_VAR] = BASE + "/zillow/zip_rents.csv"
os.environ[md.STREETEASY_VAR] = BASE + "/streeteasy/"
served = {BASE + "/streeteasy/" + k: v for k, v in fx.streeteasy("2026-10").items()}
served.update({BASE + "/zillow/" + k: v for k, v in ZI.items()})
check("ten files are requested, at the published layout", lambda: sorted(u for f in md.sources().values() for u in f.values()) == sorted(served))


def getter(files, fail=None):
    def get(url):
        if fail and fail in url:
            raise OSError("HTTP Error 503")
        return files[url]
    return get


newer = {BASE + "/streeteasy/" + k: v for k, v in fx.streeteasy("2026-11").items()}
newer.update({BASE + "/zillow/" + k: v for k, v in fx.zillow("2026-11").items()})
older = dict(newer, **{BASE + "/zillow/zip_rents.csv": ZI["zip_rents.csv"]})
with tempfile.TemporaryDirectory() as d:
    check("first refresh stores both sources", lambda: md.refresh(d, getter(served)) == []
          and loads(d, "zillow") == loads(d, "streeteasy") == "2026-10")

    def one_fails():
        f = md.refresh(d, getter(newer, fail="rentalInventory_TwoBd"))
        return (len(f) == 1 and f[0].startswith("streeteasy: rentalInventory_TwoBd.zip: HTTP Error 503")
                and loads(d, "streeteasy") == "2026-10" and loads(d, "zillow") == "2026-11")
    check("one failed file keeps that whole source and updates the other", one_fails)

    def rejected():
        f = md.refresh(d, getter(older))
        return (len(f) == 1 and "before the stored" in f[0]
                and loads(d, "zillow") == "2026-11" and loads(d, "streeteasy") == "2026-11")
    check("a rejected file keeps the stored copy", rejected)

    def interrupted():
        z = md.source_dir(d, "zillow")
        os.rename(z, z + ".old")
        return loads(d, "zillow") == "2026-11"
    check("a swap interrupted by a crash still finds the old copy", interrupted)
    check("no leftover temporary folders after a refresh", lambda: md.refresh(d, getter(newer)) == []
          and sorted(os.listdir(os.path.join(d, "market"))) == ["streeteasy", "zillow"])


def refused():
    os.environ[md.ZILLOW_VAR] = "file:///etc/passwd"
    try:
        md.sources()
    except md.MarketDataError:
        return True


before = md.config_id()
os.environ[md.ZILLOW_VAR] += "?v=2"
check("changing an address changes the configuration ID", lambda: md.config_id() != before)
check("a non-http address is refused", refused)
del os.environ[md.ZILLOW_VAR], os.environ[md.STREETEASY_VAR]
check("no settings means no sources", lambda: md.sources() == {} and not md.configured())
print("market tests done: %d passed, %d failed" % (passed, failed))
