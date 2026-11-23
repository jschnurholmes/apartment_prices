"""Write cities/nyc_zips.json: Census ZIP areas with homes where the collector keeps listings.

A ZIP is included, whole, if any 2020 block with housing lies in it and in the
NYC counties or one of the city's areas (by the block's interior point). Its
"units" counts only those blocks' homes. Boundaries are simplified like
build_areas.py's.

usage: python3 -I nyc/build_zips.py --zctas GEOJSON --blocks SHP...
  GEOJSON: 2020 ZIP Code Tabulation Areas from TIGERweb's
  PUMA_TAD_TAZ_UGA_ZCTA service, with a ZCTA5 property. SHP: TIGER/Line 2020
  TABBLOCK20 files for NY and NJ. Requires pyshp.
"""

import json
import os
import sys

import shapefile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(1, ROOT)

import build_areas
import city_areas

OUT = os.path.join(ROOT, "cities", "nyc_zips.json")


def rings(geometry):
    polys = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    return [[tuple(p[:2]) for p in ring] for poly in polys for ring in poly]


def main(argv):
    if len(argv) < 4 or argv[0] != "--zctas" or argv[2] != "--blocks":
        sys.exit(__doc__)
    with open(argv[1]) as f:
        features = json.load(f)["features"]
    zips = []
    for feat in features:
        r = rings(feat["geometry"])
        xs, ys = [x for ring in r for x, _ in ring], [y for ring in r for _, y in ring]
        zips.append({"zip": feat["properties"]["ZCTA5"], "units": 0, "rings": r,
                     "bbox": (min(xs), min(ys), max(xs), max(ys))})
    areas = city_areas.load(os.path.join(ROOT, "cities", "nyc_areas.json"))
    homes = unplaced = 0
    for path in argv[3:]:
        sf = shapefile.Reader(path)
        names = [f[0] for f in sf.fields[1:]]
        ist, ico, ilat, ilon, ihu = (names.index(n) for n in
                                     ("STATEFP20", "COUNTYFP20", "INTPTLAT20", "INTPTLON20", "HOUSING20"))
        for rec in sf.iterRecords():
            if rec[ihu] <= 0:
                continue
            lon, lat = float(rec[ilon]), float(rec[ilat])
            if rec[ist] + rec[ico] not in build_areas.NYC_COUNTIES and not city_areas.find(areas, lon, lat):
                continue
            homes += rec[ihu]
            z = city_areas.find(zips, lon, lat)
            if z:
                z["units"] += rec[ihu]
            else:
                unplaced += rec[ihu]
    kept = sorted((z for z in zips if z["units"] > 0), key=lambda z: z["zip"])
    for z in kept:
        del z["bbox"]
        z["rings"] = [[(round(x, 5), round(y, 5)) for x, y in build_areas.simplify(ring)] for ring in z["rings"]]
    with open(OUT + ".tmp", "w") as f:
        json.dump({"zips": kept}, f, separators=(",", ":"))
    os.replace(OUT + ".tmp", OUT)
    print("%d of %d ZIPs; %d homes, %d in no ZIP" % (len(kept), len(zips), homes, unplaced))


if __name__ == "__main__":
    main(sys.argv[1:])
