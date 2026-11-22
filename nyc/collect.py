"""Monthly RentCast collection run for the NYC rent map.

Runs once per RentCast billing period, on or after the day after the reset.
Before any call is sent, the calls it needs are reserved in
rentcast_quota.json and that reservation is committed and pushed; the local
run ledger records the pushed commit, so a restart never reserves twice.  A
call is marked "sending" before it goes out; after a crash such a call counts
as spent, and its page is re-sent only if the reservation still has room.

Passes run in the order listed in collect_config.json.  A pass whose pages
don't fit the remaining quota keeps only what it already fetched.  Any non-200
response stops the run (errors aren't billed, but the next call would fail too).

Environment:
  RENTCAST_API_KEY      RentCast API key
  NYC_RENTMAP_DATA_DIR  raw responses (raw/), run ledgers (runs/), and
                        Jeff's v3 scrape files (v3/apts-<ts>.txt)

Usage:
  python3 -I collect.py run      collect if this billing period still needs it
  python3 -I collect.py status   show the period, quota, and this period's run
"""

import datetime
import fcntl
import gzip
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rentcast_client
import rentcast_quota

CONFIG = os.path.join(HERE, "collect_config.json")
NYC_COUNTIES = {"36005", "36047", "36061", "36081", "36085"}


class RunStop(Exception):
    pass


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def http_get(url, key):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "X-Api-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Git:
    def __init__(self, repo):
        self.repo = repo
        self.branch = self.out("rev-parse", "--abbrev-ref", "HEAD")

    def run(self, *args, check=True):
        p = subprocess.run(["git", "-C", self.repo] + list(args),
                           capture_output=True, text=True, timeout=300)
        if check and p.returncode:
            raise RunStop("git %s failed: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
        return p

    def out(self, *args):
        return self.run(*args).stdout.strip()

    def dirty(self, path):
        return bool(self.out("status", "--porcelain", "--", path))

    def sync(self):
        """Bring the branch level with origin, pushing any local quota commits."""
        self.run("fetch", "-q", "origin")
        upstream = "origin/" + self.branch
        ahead = int(self.out("rev-list", "--count", upstream + "..HEAD"))
        behind = int(self.out("rev-list", "--count", "HEAD.." + upstream))
        if ahead and behind:
            if self.run("rebase", upstream, check=False).returncode:
                self.run("rebase", "--abort", check=False)
                raise RunStop("local quota commits conflict with origin; resolve by hand")
        elif behind:
            self.run("merge", "-q", "--ff-only", upstream)
        if ahead:
            self.run("push", "-q", "origin", "HEAD:" + self.branch)

    def commit_and_push(self, path, message):
        if self.dirty(path):
            self.run("commit", "-q", "-m", message, "--", path)
        try:
            self.run("push", "-q", "origin", "HEAD:" + self.branch)
        except RunStop:
            self.sync()
        sha = self.out("rev-parse", "HEAD")
        if not self.on_origin(sha):
            raise RunStop("commit %s is not on origin after pushing" % sha)
        return sha

    def on_origin(self, sha):
        self.run("fetch", "-q", "origin")
        return self.run("merge-base", "--is-ancestor", sha, "origin/" + self.branch,
                        check=False).returncode == 0


class Ledger:
    """Local, uncommitted record of one billing period's run."""

    def __init__(self, data_dir, period_start):
        self.path = os.path.join(data_dir, "runs", "%s.json" % period_start)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.s = json.load(f)
        else:
            self.s = {"period_start": period_start, "scrape_ts": None,
                      "pushed": [], "passes": {}, "calls": []}

    def save(self):
        atomic_write(self.path, json.dumps(self.s, indent=1).encode())

    def billed(self):
        return sum(1 for c in self.s["calls"]
                   if c["state"] == "lost" or (c["state"] == "done" and c["status"] == 200))

    def lost_offsets(self, name):
        return {c["offset"] for c in self.s["calls"] if c["pass"] == name and c["state"] == "lost"}


class Collector:
    def __init__(self, repo, data_dir, key, today=None, http=http_get):
        with open(CONFIG) as f:
            self.cfg = json.load(f)
        self.git = Git(repo)
        self.quota_path = os.path.join(HERE, "rentcast_quota.json")
        self.quota_rel = os.path.relpath(self.quota_path, repo)
        self.data_dir = data_dir
        self.key = key
        self.today = today or rentcast_quota._today()
        self.http = http

    def quota(self):
        return rentcast_quota.Quota(self.quota_path)

    def run(self):
        if self.git.dirty(self.quota_rel):
            self.git.run("commit", "-q", "-m", "Commit quota changes left by an interrupted run",
                         "--", self.quota_rel)
        self.git.sync()
        q = self.quota()
        start, _ = q.period(self.today)
        if self.today <= start:
            print("billing period starts %s; waiting until the day after the reset" % start)
            return
        self.res_id = "run-" + start.isoformat()
        self.ledger = Ledger(self.data_dir, start.isoformat())
        led = self.ledger.s
        r = q.reservation(self.res_id)
        if r and r["status"] == "closed":
            if led["scrape_ts"] and not os.path.exists(self.scrape_path()):
                self.write_scrape()
            print("period %s already collected: %d calls spent" % (start, r["spent"]))
            return
        for c in led["calls"]:
            if c["state"] == "sending":
                c["state"] = "lost"
        if led["pushed"] and not self.git.on_origin(led["pushed"][-1]["sha"]):
            raise RunStop("reservation commit %s is missing from origin" % led["pushed"][-1]["sha"])
        led["scrape_ts"] = led["scrape_ts"] or int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        self.ledger.save()
        self.raw_dir = os.path.join(self.data_dir, "raw", start.isoformat())
        os.makedirs(self.raw_dir, exist_ok=True)

        stopped = None
        try:
            for p in self.cfg["passes"]:
                self.run_pass(p)
        except RunStop as e:
            stopped = str(e)

        spent = self.ledger.billed()
        for name, st in led["passes"].items():
            print("  %-15s %-10s total=%s pages=%s" % (name, st["status"], st.get("total"), st.get("pages")))
        if stopped:
            print("spent %d calls so far for period %s; reservation left open for the next run"
                  % (spent, start))
            raise RunStop(stopped)
        self.write_scrape()
        self.prune_raw()
        q = self.quota()
        if q.reservation(self.res_id) is None:
            q.reserve(self.res_id, 0, when=self.today)
        q.close(self.res_id, spent)
        self.git.commit_and_push(self.quota_rel, "Close RentCast reservation for %s: %d calls spent"
                                 % (start, spent))
        print("spent %d calls for period %s" % (spent, start))

    def ensure_reserved(self, total):
        q = self.quota()
        r = q.reservation(self.res_id)
        if r and r["reserved"] >= total:
            return True
        try:
            q.reserve(self.res_id, total, keep_free=self.cfg["keep_free"], when=self.today)
        except rentcast_quota.QuotaError as e:
            print("  quota: %s" % e)
            return False
        sha = self.git.commit_and_push(self.quota_rel, "Reserve %d RentCast calls for %s"
                                       % (total, self.ledger.s["period_start"]))
        self.ledger.s["pushed"].append({"reserved": total, "sha": sha, "at": now_iso()})
        self.ledger.save()
        q = self.quota()
        if q.used(self.today) > q.state["monthly_limit"] - self.cfg["keep_free"]:
            raise RunStop("after syncing with origin, usage is %d of %d (keeping %d free); not spending"
                          % (q.used(self.today), q.state["monthly_limit"], self.cfg["keep_free"]))
        return True

    def room(self):
        r = self.quota().reservation(self.res_id)
        return (r["reserved"] if r else 0) - self.ledger.billed()

    def run_pass(self, p):
        st = self.ledger.s["passes"].setdefault(p["name"], {"status": "pending", "done": []})
        if st["status"] != "pending":
            return
        size = self.cfg["page_size"]
        if "total" not in st:
            if 0 in self.ledger.lost_offsets(p["name"]) and self.room() < 1:
                st["status"] = "incomplete"
                return self.ledger.save()
            if not self.ensure_reserved(self.ledger.billed() + 1):
                st["status"] = "skipped"
                return self.ledger.save()
            total = self.call(p, 0)
            st["total"] = total
            st["pages"] = max(1, math.ceil(total / size))
            st["done"].append(0)
            self.ledger.save()
        todo = [o for o in range(0, st["pages"] * size, size) if o not in st["done"]]
        lost = self.ledger.lost_offsets(p["name"])
        fresh = [o for o in todo if o not in lost]
        resend = [o for o in todo if o in lost]
        if fresh and not self.ensure_reserved(self.ledger.billed() + len(fresh)):
            st["status"] = "incomplete"
            return self.ledger.save()
        for o in fresh:
            self.call(p, o)
            st["done"].append(o)
            self.ledger.save()
        for o in resend:
            if self.room() < 1:
                break
            self.call(p, o)
            st["done"].append(o)
            self.ledger.save()
        st["status"] = "complete" if len(st["done"]) >= st["pages"] else "incomplete"
        self.ledger.save()

    def call(self, p, offset):
        circle = self.cfg["circles"][p["circle"]]
        params = {"limit": str(self.cfg["page_size"]), "includeTotalCount": "true",
                  "latitude": str(circle["latitude"]), "longitude": str(circle["longitude"]),
                  "radius": str(circle["radius"]), "status": p["status"],
                  "daysOld": "1:%d" % self.cfg["window_days"], "offset": str(offset)}
        rentcast_client.validate(params)
        if self.room() < 1:
            raise RunStop("internal error: no reserved call left for %s offset %d" % (p["name"], offset))
        c = {"n": len(self.ledger.s["calls"]) + 1, "pass": p["name"], "offset": offset,
             "state": "sending", "started": now_iso()}
        self.ledger.s["calls"].append(c)
        self.ledger.save()
        try:
            status, headers, body = self.http(rentcast_client.request_url(params), self.key)
        except Exception as e:
            c["state"] = "lost"
            self.ledger.save()
            raise RunStop("%s offset %d: no response (%r); counted as spent" % (p["name"], offset, e))
        path = os.path.join(self.raw_dir, "%s-%05d.json.gz" % (p["name"], offset))
        atomic_write(path, gzip.compress(body))
        total = headers.get("X-Total-Count") or headers.get("x-total-count")
        c.update(state="done", status=status, file=os.path.relpath(path, self.data_dir),
                 total=total, finished=now_iso())
        self.ledger.save()
        if status != 200:
            raise RunStop("%s offset %d: HTTP %d %s" % (p["name"], offset, status, body[:200]))
        return int(total) if total is not None else len(json.loads(body))

    def write_scrape(self):
        listings = {}
        for c in self.ledger.s["calls"]:
            if c["state"] != "done" or c["status"] != 200:
                continue
            with gzip.open(os.path.join(self.data_dir, c["file"])) as f:
                for r in json.load(f):
                    if (r.get("stateFips", "") + r.get("countyFips", "")) not in NYC_COUNTIES:
                        continue
                    if None in (r.get("price"), r.get("bedrooms"), r.get("latitude"), r.get("longitude")):
                        continue
                    listings[re.sub(r"\s+", "-", r["id"])] = r
        lines = ["# RentCast, billing period %s, %d NYC listings\n"
                 % (self.ledger.s["period_start"], len(listings))]
        for lid, r in sorted(listings.items()):
            lines.append("%d %d %s %.6f %.6f\n" % (round(r["price"]), int(r["bedrooms"]), lid,
                                                   r["longitude"], r["latitude"]))
        path = self.scrape_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        atomic_write(path, "".join(lines).encode())
        print("wrote %s (%d NYC listings)" % (path, len(listings)))

    def scrape_path(self):
        return os.path.join(self.data_dir, "v3", "apts-%d.txt" % self.ledger.s["scrape_ts"])

    def prune_raw(self):
        y, m = rentcast_quota._add_months(self.today.year, self.today.month, -self.cfg["raw_retention_months"])
        cutoff = datetime.date(y, m, 1)
        raw_root = os.path.join(self.data_dir, "raw")
        for name in os.listdir(raw_root):
            try:
                if datetime.date.fromisoformat(name) < cutoff:
                    shutil.rmtree(os.path.join(raw_root, name))
                    print("pruned raw responses for %s" % name)
            except ValueError:
                continue


def main(argv):
    if not argv or argv[0] not in ("run", "status"):
        sys.exit(__doc__)
    data_dir = os.environ.get("NYC_RENTMAP_DATA_DIR")
    if not data_dir:
        sys.exit("NYC_RENTMAP_DATA_DIR is not set")
    os.makedirs(data_dir, exist_ok=True)
    repo = subprocess.run(["git", "-C", HERE, "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, check=True).stdout.strip()
    if argv[0] == "status":
        rentcast_quota.main(["status"])
        q = rentcast_quota.Quota()
        start, _ = q.period()
        path = os.path.join(data_dir, "runs", "%s.json" % start)
        if os.path.exists(path):
            with open(path) as f:
                s = json.load(f)
            for name, st in s["passes"].items():
                print("  %-15s %-10s total=%s pages=%s" % (name, st["status"], st.get("total"), st.get("pages")))
        else:
            print("  no run yet this period")
        return
    key = os.environ.get("RENTCAST_API_KEY")
    if not key:
        sys.exit("RENTCAST_API_KEY is not set")
    with open(os.path.join(data_dir, "collect.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another collection run is in progress")
        try:
            Collector(repo, data_dir, key).run()
        except (RunStop, rentcast_quota.QuotaError) as e:
            sys.exit("run stopped: %s" % e)


if __name__ == "__main__":
    main(sys.argv[1:])
