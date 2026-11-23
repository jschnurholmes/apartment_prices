import datetime
import json
import os
import subprocess
import sys
import urllib.parse

pi_repo, data_dir, scenario, today = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sys.path.insert(0, os.path.join(pi_repo, "nyc"))
import collect
import rentcast_quota

TOTALS = {("Active", "12.65"): 6557, ("Active", "7.05"): 333,
          ("Inactive", "12.65"): 3029, ("Inactive", "7.05"): 153}
log_path = os.path.join(data_dir, "fake_calls.log")


class Crash(BaseException):
    pass


def fake(url, key, crash_at=None, fail_status=None):
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    with open(log_path, "a") as f:
        f.write(json.dumps(q) + "\n")
    n = sum(1 for _ in open(log_path))
    if crash_at and n == crash_at:
        raise Crash("simulated power loss during call %d" % n)
    if fail_status:
        return fail_status, {}, b'{"error": "billing/subscription-inactive"}'
    total = TOTALS[(q["status"], q["radius"])]
    off = int(q["offset"])
    recs = []
    for i in range(off, min(off + 500, total)):
        nj = i % 5 == 0
        recs.append({"id": "%s-%s-%s-%d Main St, Apt %d" % (today[:7], q["status"], q["radius"], i, i),
                     "stateFips": "34" if nj else "36", "countyFips": "017" if nj else "047",
                     "price": 2000 + i % 3000 + int(today[5:7]), "bedrooms": i % 4,
                     "latitude": 40.65 + (i % 100) / 10000, "longitude": -73.95})
    return 200, {"X-Total-Count": str(total)}, json.dumps(recs).encode()


kw = {}
if scenario.startswith("crash@"):
    kw["crash_at"] = int(scenario.split("@")[1])
if scenario == "http403":
    kw["fail_status"] = 403

if scenario == "mainpublish":
    os.environ["NYC_RENTMAP_DATA_DIR"] = data_dir
    try:
        collect.main(["publish"])
        print("RESULT ok")
    except SystemExit as e:
        print("RESULT stopped:", e)
    sys.exit(0)

c = None if scenario == "publish" else collect.Collector(
    pi_repo, data_dir, "dummy-key", today=datetime.date.fromisoformat(today),
    http=lambda u, k: fake(u, k, **kw))
try:
    if c is None:
        collect.build_and_publish(pi_repo, data_dir, collect.Git(pi_repo), "manual rebuild")
    else:
        c.run()
    print("RESULT ok")
except collect.RunStop as e:
    print("RESULT stopped:", e)
except Crash as e:
    print("RESULT crashed:", e)
