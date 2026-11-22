"""Quota-guarded client for RentCast's /listings/rental/long-term endpoint.

Every request is recorded in rentcast_ledger.json before it is sent, so failed
or timed-out attempts still count.  Requests are refused once the ledger holds
`limit` calls.  Requests are never retried, and every response is saved under
data/raw/ so nothing has to be fetched twice.

Usage:
  python3 -I rentcast_client.py --dry-run key=value ...   # print request, send nothing
  python3 -I rentcast_client.py --send    key=value ...   # spend one call
  python3 -I rentcast_client.py --status                  # show ledger

The API key is read from the RENTCAST_API_KEY environment variable.
"""

import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "rentcast_ledger.json")
RAW_DIR = os.path.join(HERE, "data", "raw")
ENDPOINT = "https://api.rentcast.io/v1/listings/rental/long-term"
DEFAULT_LIMIT = 10

ALLOWED = {
    "address", "city", "state", "zipCode", "latitude", "longitude", "radius",
    "propertyType", "bedrooms", "bathrooms", "squareFootage", "lotSize",
    "yearBuilt", "status", "price", "daysOld", "limit", "offset",
    "includeTotalCount",
}
RANGE = re.compile(r"^\d+(\.\d+)?(:\d+(\.\d+)?)?$")


def load_ledger():
    if not os.path.exists(LEDGER):
        return {"limit": DEFAULT_LIMIT, "calls": []}
    with open(LEDGER) as f:
        return json.load(f)


def save_ledger(ledger):
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LEDGER)


def build_params(args):
    params = {"limit": "500", "includeTotalCount": "true"}
    for arg in args:
        if "=" not in arg:
            sys.exit("bad argument %r: expected key=value" % arg)
        key, value = arg.split("=", 1)
        if key not in ALLOWED:
            sys.exit("unknown parameter %r" % key)
        params[key] = value
    validate(params)
    return params


def validate(p):
    if not 1 <= int(p["limit"]) <= 500:
        sys.exit("limit must be 1-500")
    if "radius" in p and not 0 < float(p["radius"]) <= 100:
        sys.exit("radius must be 0-100 miles")
    if ("latitude" in p) != ("longitude" in p):
        sys.exit("latitude and longitude go together")
    if "radius" in p and "latitude" not in p and "address" not in p:
        sys.exit("radius needs latitude/longitude or address")
    has_location = any(k in p for k in ("address", "city", "state", "zipCode", "latitude"))
    if not has_location:
        sys.exit("no location given; the API would fall back to its defaults")
    if p.get("status", "Active") not in ("Active", "Inactive"):
        sys.exit("status must be Active or Inactive")
    for k in ("daysOld", "price", "bedrooms", "bathrooms"):
        if k in p and not all(RANGE.match(v) for v in p[k].split(",")):
            sys.exit("%s=%r is not a number, range (a:b), or list" % (k, p[k]))


def request_url(params):
    return ENDPOINT + "?" + urllib.parse.urlencode(params)


def send(params):
    key = os.environ.get("RENTCAST_API_KEY")
    if not key:
        sys.exit("RENTCAST_API_KEY is not set; nothing sent")

    ledger = load_ledger()
    used = len(ledger["calls"])
    if used >= ledger["limit"]:
        sys.exit("quota reached: %d of %d calls used; nothing sent" % (used, ledger["limit"]))

    n = used + 1
    entry = {
        "n": n,
        "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "params": params,
        "result": "pending",
    }
    ledger["calls"].append(entry)
    save_ledger(ledger)

    os.makedirs(RAW_DIR, exist_ok=True)
    body_path = os.path.join(RAW_DIR, "call-%02d.json" % n)
    req = urllib.request.Request(
        request_url(params),
        headers={"Accept": "application/json", "X-Api-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status, headers, body = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        status, headers, body = e.code, dict(e.headers), e.read()
    except Exception as e:
        entry["result"] = "error: %r" % (e,)
        save_ledger(ledger)
        sys.exit("call %d failed before a response: %r" % (n, e))

    with open(body_path, "wb") as f:
        f.write(body)
    entry["result"] = status
    entry["total_count"] = headers.get("X-Total-Count") or headers.get("x-total-count")
    entry["file"] = os.path.relpath(body_path, HERE)
    try:
        records = json.loads(body)
        entry["records"] = len(records) if isinstance(records, list) else None
    except ValueError:
        entry["records"] = None
    save_ledger(ledger)

    print("call %d of %d: HTTP %s, %s records, X-Total-Count=%s, saved %s"
          % (n, ledger["limit"], status, entry["records"], entry["total_count"], entry["file"]))


def status():
    ledger = load_ledger()
    print("%d of %d calls used" % (len(ledger["calls"]), ledger["limit"]))
    for c in ledger["calls"]:
        print("  #%d %s result=%s records=%s total=%s %s" % (
            c["n"], c["started"], c["result"], c.get("records"),
            c.get("total_count"), json.dumps(c["params"])))


def main(argv):
    if argv[:1] == ["--status"]:
        status()
    elif argv[:1] == ["--dry-run"]:
        params = build_params(argv[1:])
        ledger = load_ledger()
        print("would send call %d of %d:" % (len(ledger["calls"]) + 1, ledger["limit"]))
        print("  GET " + request_url(params))
    elif argv[:1] == ["--send"]:
        send(build_params(argv[1:]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
