"""Write the site's Zillow and StreetEasy data from the files market_data.py downloaded.

Runs after build_rentmap.py, whose cpi.csv and city.json it uses. A source
that isn't configured or has no stored files leaves nothing in the site;
city.json's "market" lists the ones that do.

usage: RENTMAP_CITY=nyc python3 nyc/build_market.py DATA_DIR SITE_DIR
"""

import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.dirname(HERE))
import build_rentmap
import market_data

ZIPS = os.path.join(os.path.dirname(HERE), "cities", "nyc_zips.json")
POSITIONS = os.path.join(os.path.dirname(HERE), "cities", "nyc_streeteasy.json")


def rounded(v):
    return None if v is None else int(round(v / 10.0)) * 10


def cpi_fields(cpi, months):
    return {"cpi": [build_rentmap.cpi_for(cpi, m + "-01") for m in months],
            "cpi_latest": cpi[max(cpi)] if cpi else None,
            "cpi_latest_month": max(cpi) if cpi else None}


def zillow(data, cpi):
    """The map's ZIPs with any rent; "city" is their median weighted by homes, like the rent map's."""
    with open(ZIPS) as f:
        areas = json.load(f)["zips"]
    zips = [dict(zip=a["zip"], units=a["units"], rings=a["rings"], rent=data["zips"][a["zip"]]["rent"])
            for a in areas if a["zip"] in data["zips"]]
    zips = [z for z in zips if any(v is not None for v in z["rent"])]
    city = []
    for t in range(len(data["months"])):
        have = [z for z in zips if z["rent"][t] is not None]
        city.append(rounded(build_rentmap.rentmap._wmedian(np.array([z["rent"][t] for z in have]),
                                                           np.array([z["units"] for z in have]))) if have else None)
    return dict(cpi_fields(cpi, data["months"]), months=data["months"], city=city,
                zips=[{"zip": z["zip"], "rent": [rounded(v) for v in z["rent"]], "rings": z["rings"]} for z in zips])


def streeteasy(data, cpi):
    """Rents are in $10 steps; a negative one is a 3-month figure from too few listings.

    "pos" is where a neighborhood's dot goes; one without a known position gets no dot.
    """
    with open(POSITIONS) as f:
        pos = {(p["borough"], p["name"]): p["pos"] for p in json.load(f)["neighborhoods"]}
    areas, lost = [], []
    for a in data["areas"]:
        rent = {}
        for b in sorted(a["rent"]):
            rent[str(b)] = [None if v is None else -rounded(v) if thin else rounded(v)
                            for v, thin in market_data.pooled(a["rent"][b], a["n"][b])]
        areas.append({"name": a["name"], "borough": a["borough"], "kind": a["kind"], "rent": rent,
                      "n": {str(b): [int(n or 0) for n in a["n"][b]] for b in sorted(a["n"])}})
        if a["kind"] == "neighborhood":
            if (a["borough"], a["name"]) in pos:
                areas[-1]["pos"] = pos[a["borough"], a["name"]]
            else:
                lost.append(a["name"])
    if lost:
        print("market data: no map position for %s" % ", ".join(lost))
    index = data["index"]
    return dict(cpi_fields(cpi, data["months"]), months=data["months"], min_listings=market_data.MIN_LISTINGS,
                areas=areas, index=dict(months=index["months"],
                                        cpi=cpi_fields(cpi, index["months"])["cpi"],
                                        series={k: [rounded(v) for v in s] for k, s in index["series"].items()}))


def main(data_dir, site):
    cpi = build_rentmap.read_cpi(site)
    built = []
    for source, build in (("zillow", zillow), ("streeteasy", streeteasy)):
        path = os.path.join(site, source + ".json")
        data = market_data.stored(data_dir, source) if source in market_data.sources() else None
        if data is None:
            if os.path.exists(path):
                os.remove(path)
            continue
        build_rentmap.atomic_write(path, json.dumps(build(data, cpi), separators=(",", ":")))
        built.append(source)
    if built:
        path = os.path.join(site, "city.json")
        with open(path) as f:
            city = json.load(f)
        city["market"] = built
        build_rentmap.atomic_write(path, json.dumps(city))
        print("market data: built %s" % ", ".join(built))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
