import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
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
