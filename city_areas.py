"""Municipal boundaries a city's map covers beyond its counties.

The file named by a city's "areas" setting (e.g. cities/nyc_areas.json) holds
{"areas": [{"geoid", "name", "state", "housing_units", "label": [lat, lon],
"rings": [[[lon, lat], ...], ...]}]}. A point is in an area if it falls inside
an odd number of the area's rings, so holes and separate parts both work.
"""

import json


def load(path):
    with open(path) as f:
        areas = json.load(f)["areas"]
    for a in areas:
        xs = [x for ring in a["rings"] for x, _ in ring]
        ys = [y for ring in a["rings"] for _, y in ring]
        a["bbox"] = (min(xs), min(ys), max(xs), max(ys))
    return areas


def find(areas, lon, lat):
    """Return the area containing (lon, lat), or None."""
    for a in areas:
        x0, y0, x1, y1 = a["bbox"]
        if x0 <= lon <= x1 and y0 <= lat <= y1 and inside(a["rings"], lon, lat):
            return a
    return None


def inside(rings, lon, lat):
    result = False
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                result = not result
    return result
