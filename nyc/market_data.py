"""Published rent data shown beside the RentCast map: Zillow by ZIP, StreetEasy by neighborhood.

Files are kept in <data dir>/market/<source>/, the last good copy of each.

Environment (a source without its variable is off):
  NYC_RENTMAP_ZILLOW_ZIP_RENTS_URL  Zillow's ZIP-level rent index CSV
  NYC_RENTMAP_STREETEASY_RENTALS_URL  folder of StreetEasy's rental files, holding
                                      <size>/medianAskingRent_<size>.zip,
                                      <size>/rentalInventory_<size>.zip and
                                      All/rentalIndex_All.zip
"""

import csv
import hashlib
import http.client
import io
import os
import re
import shutil
import urllib.request
import zipfile
import zlib

ZILLOW_VAR = "NYC_RENTMAP_ZILLOW_ZIP_RENTS_URL"
STREETEASY_VAR = "NYC_RENTMAP_STREETEASY_RENTALS_URL"
SIZES = {"Studio": 0, "OneBd": 1, "TwoBd": 2, "ThreePlusBd": 3}
NYC_COUNTIES = {"Bronx County", "Kings County", "New York County", "Queens County", "Richmond County"}
BOROUGHS = {"Bronx", "Brooklyn", "Manhattan", "Queens", "Staten Island"}
INDEX_AREAS = ("NYC", "Manhattan", "Brooklyn", "Queens")
MAX_BYTES = 100_000_000
MIN_NYC_ZIPS = 100
MIN_NEIGHBORHOODS = 100
MIN_LISTINGS = 5
POOL_MONTHS = 3


class MarketDataError(Exception):
    pass


def sources():
    """{source: {filename: url}} for the sources that are configured."""
    out = {}
    url = os.environ.get(ZILLOW_VAR, "").strip()
    if url:
        out["zillow"] = {"zip_rents.csv": checked_url(ZILLOW_VAR, url)}
    base = os.environ.get(STREETEASY_VAR, "").strip().rstrip("/")
    if base:
        checked_url(STREETEASY_VAR, base)
        files = {"rentalIndex_All.zip": base + "/All/rentalIndex_All.zip"}
        for size in SIZES:
            for metric in ("medianAskingRent", "rentalInventory"):
                name = "%s_%s.zip" % (metric, size)
                files[name] = "%s/%s/%s" % (base, size, name)
        out["streeteasy"] = files
    return out


def checked_url(var, url):
    if not re.fullmatch(r"https?://[^\s\"<>]+", url):
        raise MarketDataError("%s must be an http(s) URL, got %r" % (var, url))
    return url


def configured():
    return any(os.environ.get(v, "").strip() for v in (ZILLOW_VAR, STREETEASY_VAR))


def config_id():
    """Identifies the configured files, so a changed setting is fetched again."""
    return hashlib.sha256(repr(sorted((s, sorted(f.items())) for s, f in sources().items())).encode()).hexdigest()[:16]


def http_get(url):
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise MarketDataError("larger than %d bytes" % MAX_BYTES)
    return data


def source_dir(data_dir, source):
    return os.path.join(data_dir, "market", source)


def read_files(directory, names):
    out = {}
    for name in names:
        with open(os.path.join(directory, name), "rb") as f:
            out[name] = f.read()
    return out


def refresh(data_dir, get=http_get):
    """Download each configured source, replacing its files only if all of them pass.

    Returns a list of failure messages, one per source that kept its old files.
    """
    failures = []
    for source, files in sources().items():
        try:
            new = {}
            for name, url in files.items():
                try:
                    new[name] = get(url)
                except (OSError, http.client.HTTPException, MarketDataError) as e:
                    raise MarketDataError("%s: %s" % (name, e))
            check(source, new, load_previous(data_dir, source))
            replace(source_dir(data_dir, source), new)
            print("market data: %s updated through %s" % (source, latest(source, new)))
        except MarketDataError as e:
            failures.append("%s: %s" % (source, e))
            print("market data: %s kept as it was: %s" % (source, e))
    return failures


def replace(directory, files):
    tmp = directory + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for name, data in files.items():
        with open(os.path.join(tmp, name), "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    old = directory + ".old"
    if os.path.exists(directory):
        shutil.rmtree(old, ignore_errors=True)
        os.rename(directory, old)
    os.rename(tmp, directory)
    shutil.rmtree(old, ignore_errors=True)


def load_previous(data_dir, source):
    """The stored files of a source, or None if there are none that still parse."""
    directory = source_dir(data_dir, source)
    for d in (directory, directory + ".old"):
        if os.path.isdir(d):
            try:
                files = read_files(d, sorted(os.listdir(d)))
                parse(source, files)
                return files
            except (OSError, KeyError, MarketDataError):
                continue
    return None


def stored(data_dir, source):
    """A source's parsed stored data, or None."""
    files = load_previous(data_dir, source)
    return parse(source, files) if files else None


def parse(source, files):
    return parse_zillow(files) if source == "zillow" else parse_streeteasy(files)


def latest(source, files):
    return parse(source, files)["months"][-1]


def check(source, new, old):
    """Raise MarketDataError if the new files are implausible on their own or beside the old ones."""
    p = parse(source, new)
    count = coverage(source, p)
    if source == "zillow" and count < MIN_NYC_ZIPS:
        raise MarketDataError("only %d NYC ZIPs have a rent for %s" % (count, p["months"][-1]))
    if source == "streeteasy" and count < MIN_NEIGHBORHOODS:
        raise MarketDataError("only %d neighborhoods have a median for %s" % (count, p["months"][-1]))
    if old:
        q = parse(source, old)
        if p["months"][-1] < q["months"][-1]:
            raise MarketDataError("ends at %s, before the stored copy's %s" % (p["months"][-1], q["months"][-1]))
        if p["months"][0] > q["months"][0]:
            raise MarketDataError("starts at %s, after the stored copy's %s" % (p["months"][0], q["months"][0]))
        if count < coverage(source, q) * 0.8:
            raise MarketDataError("covers %d areas, far fewer than the stored copy's %d" % (count, coverage(source, q)))


def coverage(source, p):
    if source == "zillow":
        return sum(1 for z in p["zips"].values() if z["nyc"] and z["rent"][-1] is not None)
    return sum(1 for a in p["areas"] if a["kind"] == "neighborhood"
               and any(a["rent"][s][-1] is not None for s in a["rent"]))


def pooled(rent, n):
    """[(value, thin)] per month for one area and size.

    A month with MIN_LISTINGS or more is its own median. A thinner one is the
    average of the latest POOL_MONTHS months that have a median, weighted by
    their listing counts, and marked thin.
    """
    out = []
    for t in range(len(rent)):
        if rent[t] is not None and (n[t] or 0) >= MIN_LISTINGS:
            out.append((rent[t], False))
            continue
        have = [k for k in range(max(0, t - POOL_MONTHS + 1), t + 1) if rent[k] is not None and n[k]]
        total = sum(n[k] for k in have)
        out.append((sum(rent[k] * n[k] for k in have) / total, True) if have else (None, False))
    return out


def number(text):
    if text is None or not text.strip():
        return None
    try:
        v = float(text)
    except ValueError:
        raise MarketDataError("not a number: %r" % text[:40])
    if not 0 <= v < 1e7:
        raise MarketDataError("out of range: %r" % text[:40])
    return v


def table(data, required):
    try:
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error) as e:
        raise MarketDataError("unreadable CSV: %s" % e)
    missing = set(required) - set(rows[0] if rows else ())
    if missing:
        raise MarketDataError("missing columns: %s" % ", ".join(sorted(missing)))
    if any(None in r or None in r.values() for r in rows):
        raise MarketDataError("a row's length doesn't match the header")
    return rows


def unzip(name, data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = [m for m in zf.infolist() if m.filename.endswith(".csv")]
            if len(members) != 1:
                raise MarketDataError("%s: expected one CSV inside" % name)
            if members[0].file_size > MAX_BYTES:
                raise MarketDataError("%s: larger than %d bytes inside" % (name, MAX_BYTES))
            return zf.read(members[0])
    except (zipfile.BadZipFile, zlib.error, NotImplementedError, RuntimeError, EOFError) as e:
        raise MarketDataError("%s: unreadable zip: %s" % (name, e))


def consecutive(months, what):
    """Months as YYYY-MM, checked to be a complete run."""
    if not months:
        raise MarketDataError("%s: no month columns" % what)
    y, m = int(months[0][:4]), int(months[0][5:7])
    for month in months:
        if month != "%04d-%02d" % (y, m):
            raise MarketDataError("%s: months aren't consecutive at %s" % (what, month))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def parse_zillow(files):
    """{"months": [YYYY-MM], "zips": {zip: {"nyc": bool, "rent": [dollars or None]}}} for NY and NJ."""
    rows = table(files["zip_rents.csv"], ("RegionName", "RegionType", "State", "CountyName"))
    cols = [c for c in rows[0] if re.fullmatch(r"\d{4}-\d\d-\d\d", c)]
    months = consecutive([c[:7] for c in cols], "Zillow")
    zips = {}
    for r in rows:
        if r["RegionType"] != "zip" or r["State"] not in ("NY", "NJ"):
            continue
        zips[r["RegionName"].zfill(5)] = {"nyc": r["State"] == "NY" and r["CountyName"] in NYC_COUNTIES,
                                          "rent": [number(r[c]) for c in cols]}
    return {"months": months, "zips": zips}


def parse_streeteasy(files):
    """{"months", "areas": [{"name", "borough", "kind", "rent": {size: [...]}, "n": {size: [...]}}],
    "index": {"months", "series": {area: [...]}}}"""
    months, areas = None, {}
    for size, b in SIZES.items():
        for metric, field in (("medianAskingRent", "rent"), ("rentalInventory", "n")):
            name = "%s_%s.zip" % (metric, size)
            rows = table(unzip(name, files[name]), ("areaName", "Borough", "areaType"))
            cols = [c for c in rows[0] if re.fullmatch(r"\d{4}-\d\d", c)]
            if months is None:
                months = consecutive(cols, "StreetEasy")
            elif cols != months:
                raise MarketDataError("%s: its months differ from the other files'" % name)
            for r in rows:
                key = (r["areaType"], r["Borough"], r["areaName"])
                area = areas.setdefault(key, {"name": r["areaName"], "borough": r["Borough"],
                                              "kind": r["areaType"], "rent": {}, "n": {}})
                area[field][b] = [number(r[c]) for c in cols]
    for key, a in areas.items():
        if len(a["rent"]) != len(SIZES) or len(a["n"]) != len(SIZES):
            raise MarketDataError("%s isn't in every StreetEasy file" % key[2])
    named = {(a["kind"], a["name"]) for a in areas.values()}
    absent = [n for n in sorted(BOROUGHS) if ("borough", n) not in named]
    if absent or ("city", "NYC") not in named:
        raise MarketDataError("StreetEasy's files lack %s" % ", ".join(absent or ["NYC"]))
    rows = table(unzip("rentalIndex_All.zip", files["rentalIndex_All.zip"]), ("month",) + INDEX_AREAS)
    index = {"months": consecutive([r["month"] for r in rows], "StreetEasy index"),
             "series": {a: [number(r[a]) for r in rows] for a in INDEX_AREAS}}
    return {"months": months, "areas": list(areas.values()), "index": index}
