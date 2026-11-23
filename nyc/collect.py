"""Monthly RentCast collection run for the NYC rent map.

Runs once per RentCast billing period, on or after the day after the reset.
Before any call is sent, the calls it needs are reserved in
rentcast_quota.json and that reservation is committed and pushed; the local
run ledger records the pushed commit, so a restart never reserves twice.  A
call is marked "sending" before it goes out; after a crash such a call counts
as spent, and its page is re-sent only if the reservation still has room.

Passes run in the order listed in collect_config.json.  A pass marked
use_all_quota fetches as many pages as the quota allows even if it can't
finish.  Any other pass runs only if all of its pages fit; before probing its
first page, it is skipped if the previous period's total says it won't fit.
Any non-200 response stops the run (errors aren't billed, but the next call
would fail too).

After a successful collection the run publishes: it runs Jeff's v3 build with
RENTMAP_CITY=nyc into site/, and force-pushes that directory as a single
commit to the gh-pages branch (skipped if nothing changed).  If publishing
fails, the next run retries it without collecting again.

Before building, the run downloads the Zillow and StreetEasy files that are
configured (see market_data.py), once per billing period. If that fails, the
site is still published with the last good copies, then the run fails and the
next one retries.

Then, if NYC_RENTMAP_BACKUP_DIR names a clone of a backup repository, each
collected period's raw responses and ledger are committed there as
<period>.tar.xz and pushed; a failed backup is retried by the next run.
Everything else can be rebuilt from those archives: `restore` unpacks them
into the data directory, and `publish` then rewrites the scrapes and the site.

Environment:
  RENTCAST_API_KEY        RentCast API key
  NYC_RENTMAP_DATA_DIR    raw responses (raw/), run ledgers (runs/), Jeff's v3
                          scrape files (v3/apts-<ts>.txt), and the built site/
  NYC_RENTMAP_BACKUP_DIR  optional clone of the backup repository

Usage:
  python3 -I collect.py run      collect if this billing period still needs it
  python3 -I collect.py status   show the period, quota, and this period's run
  python3 -I collect.py publish  rebuild the site and push it to gh-pages
  python3 -I collect.py restore  unpack the backup clone's archives into the data directory
"""

import datetime
import fcntl
import gzip
import hashlib
import io
import json
import lzma
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(1, ROOT)
import city_areas
import market_data
import rentcast_client
import rentcast_quota

CONFIG = os.path.join(HERE, "collect_config.json")
CITY_CONFIG = os.path.join(ROOT, "cities", "nyc.json")
NYC_COUNTIES = {"36005", "36047", "36061", "36081", "36085"}
ROOM_MAX_SQFT = 200
ROOM_MAX_RENT = 2000
ROOM_MAX_RENT_PER_BEDROOM = 400
BACKUP_DIR = os.environ.get("NYC_RENTMAP_BACKUP_DIR")
LEDGER_FIELDS_NOT_BACKED_UP = ("published", "published_at", "scrape_filter", "backed_up", "market_data")


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

    def run(self, *args, check=True, env=None):
        p = subprocess.run(["git", "-C", self.repo] + list(args),
                           capture_output=True, text=True, timeout=300, env=env)
        if check and p.returncode:
            raise RunStop("git %s failed: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
        return p

    def out(self, *args):
        return self.run(*args).stdout.strip()

    def dirty(self, path):
        return bool(self.out("status", "--porcelain", "--", path))

    def sync(self):
        """Bring the branch level with origin, pushing any local quota commits.

        Returns True if commits from origin were brought in.
        """
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
        return behind > 0

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

    def publish_tree(self, directory, branch, message):
        """Force-push `directory` as a single commit on `branch`; None if unchanged.

        GitHub Pages keeps a custom domain in a CNAME file on the published
        branch, so that file is carried over from the branch's current commit.
        """
        ref = "refs/heads/" + branch
        p = self.run("ls-remote", "--exit-code", "origin", ref, check=False)
        if p.returncode not in (0, 2):
            raise RunStop("git ls-remote failed: %s" % (p.stderr or p.stdout).strip())
        old = None
        if p.returncode == 0:
            self.run("fetch", "-q", "origin", ref)
            old = self.out("rev-parse", "FETCH_HEAD")
        git_dir = self.out("rev-parse", "--absolute-git-dir")
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))

            def index(*args):
                p = subprocess.run(["git", "--git-dir", git_dir, "--work-tree", directory] + list(args),
                                   cwd=directory, env=env, capture_output=True, text=True, timeout=600)
                if p.returncode:
                    raise RunStop("git %s failed: %s" % (" ".join(args), p.stderr.strip()))
                return p.stdout.strip()

            index("add", "-A", ".")
            if old and self.run("cat-file", "-e", old + ":CNAME", check=False).returncode == 0:
                blob = self.out("rev-parse", old + ":CNAME")
                index("update-index", "--add", "--cacheinfo", "100644,%s,CNAME" % blob)
            tree = index("write-tree")
        if old and self.out("rev-parse", old + "^{tree}") == tree:
            return None
        commit = self.out("commit-tree", tree, "-m", message)
        self.run("push", "-q", "--force-with-lease=%s:%s" % (ref, old or ""),
                 "origin", "%s:%s" % (commit, ref))
        return commit


def restart_if_updated(pulled):
    """Re-run this command once after pulling new commits, so it runs the code it pulled."""
    if pulled and not os.environ.get("NYC_RENTMAP_RESTARTED"):
        print("pulled new commits; restarting", flush=True)
        sys.stderr.flush()
        os.environ["NYC_RENTMAP_RESTARTED"] = "1"
        os.execv(sys.executable, [sys.executable] + sys.orig_argv[1:])


def load_city():
    with open(CITY_CONFIG) as f:
        return json.load(f)


class ScrapeFilter:
    """Which listings a scrape keeps: those in the NYC counties or the city's areas."""

    def __init__(self):
        path = os.path.join(ROOT, load_city()["areas"])
        self.areas = city_areas.load(path)
        rules = {"counties": sorted(NYC_COUNTIES), "dedupe": "bedrooms, rent, location to 5 places",
                 "rooms": [ROOM_MAX_SQFT, ROOM_MAX_RENT, ROOM_MAX_RENT_PER_BEDROOM]}
        with open(path, "rb") as f:
            self.fingerprint = hashlib.sha256(
                json.dumps(rules, sort_keys=True).encode() + f.read()).hexdigest()[:16]

    def keep(self, r):
        if (r.get("stateFips", "") + r.get("countyFips", "")) in NYC_COUNTIES:
            return True
        return city_areas.find(self.areas, r["longitude"], r["latitude"]) is not None


def is_room(r):
    """A room in a shared house, listed under the whole house's bedroom count."""
    if r.get("squareFootage"):
        return r["squareFootage"] < ROOM_MAX_SQFT and r["price"] < ROOM_MAX_RENT
    return r["bedrooms"] >= 3 and r["price"] / r["bedrooms"] < ROOM_MAX_RENT_PER_BEDROOM


def scrape_path(data_dir, led):
    return os.path.join(data_dir, "v3", "apts-%d.txt" % led["scrape_ts"])


def write_scrape(data_dir, led, flt):
    listings = {}
    for c in led["calls"]:
        if c["state"] != "done" or c["status"] != 200:
            continue
        with gzip.open(os.path.join(data_dir, c["file"])) as f:
            for r in json.load(f):
                if None in (r.get("price"), r.get("bedrooms"), r.get("latitude"), r.get("longitude")):
                    continue
                if not flt.keep(r):
                    continue
                listings[re.sub(r"\s+", "-", r["id"])] = r
    rooms = {lid for lid, r in listings.items() if is_room(r)}
    seen, kept = set(), {}
    for lid, r in sorted(listings.items()):
        key = (int(r["bedrooms"]), round(r["price"]), round(r["latitude"], 5), round(r["longitude"], 5))
        if lid in rooms or key in seen:
            continue
        seen.add(key)
        kept[lid] = r
    dupes = len(listings) - len(rooms) - len(kept)
    listings = kept
    lines = ["# RentCast, billing period %s, %d listings\n" % (led["period_start"], len(listings))]
    for lid, r in sorted(listings.items()):
        lines.append("%d %d %s %.6f %.6f\n" % (round(r["price"]), int(r["bedrooms"]), lid,
                                               r["longitude"], r["latitude"]))
    path = scrape_path(data_dir, led)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write(path, "".join(lines).encode())
    led["scrape_filter"] = flt.fingerprint
    print("wrote %s (%d listings; dropped %d room rentals and %d same-month duplicates)"
          % (path, len(listings), len(rooms), dupes))


def refilter_scrapes(data_dir, flt):
    """Rewrite scrapes written under an older filter from their raw responses.

    Also writes the missing scrape of any period whose reservation is closed,
    as after a restore. A rewritten period's ledger loses its "published"
    mark, so the next run republishes. Returns True if any scrape was written.
    """
    closed = closed_periods()
    changed = False
    for ledger in ledgers(data_dir):
        led = ledger.s
        if not led.get("scrape_ts"):
            continue
        if os.path.exists(scrape_path(data_dir, led)):
            if led.get("scrape_filter") == flt.fingerprint:
                continue
        elif led["period_start"] not in closed:
            continue
        if not all(os.path.exists(os.path.join(data_dir, f)) for f in raw_files(led)):
            print("can't refilter period %s: its raw responses were pruned" % led["period_start"])
            continue
        write_scrape(data_dir, led, flt)
        led.pop("published", None)
        ledger.save()
        changed = True
    return changed


def ledgers(data_dir):
    runs = os.path.join(data_dir, "runs")
    names = sorted(os.listdir(runs)) if os.path.isdir(runs) else []
    return [Ledger(data_dir, n[:-len(".json")]) for n in names if n.endswith(".json")]


def closed_periods():
    return {r["period_start"] for r in rentcast_quota.Quota().state["reservations"]
            if r["status"] == "closed"}


def raw_files(led):
    return [c["file"] for c in led["calls"] if c["state"] == "done" and c["status"] == 200]


def backup_archive(data_dir, led):
    """A period's ledger and decompressed raw responses as a reproducible tar.xz."""
    entries = [("runs/%s.json" % led["period_start"],
                json.dumps({k: v for k, v in led.items() if k not in LEDGER_FIELDS_NOT_BACKED_UP},
                           indent=1, sort_keys=True).encode())]
    for rel in raw_files(led):
        with gzip.open(os.path.join(data_dir, rel)) as f:
            entries.append((rel[:-len(".gz")], f.read()))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return lzma.compress(buf.getvalue(), preset=9 | lzma.PRESET_EXTREME)


def back_up(backup_dir, data_dir, led):
    """Commit the period's archive to the backup clone if it's new, push, and return HEAD."""
    def git(*args, check=True):
        p = subprocess.run(["git", "-C", backup_dir] + list(args),
                           capture_output=True, text=True, timeout=300)
        if check and p.returncode:
            raise RunStop("backup: git %s failed: %s" % (" ".join(args), (p.stderr or p.stdout).strip()))
        return p.stdout.strip()

    branch = git("symbolic-ref", "--short", "HEAD")
    git("fetch", "-q", "origin")
    if git("rev-parse", "-q", "--verify", "origin/" + branch, check=False):
        git("merge", "-q", "--ff-only", "origin/" + branch)
    name = "%s.tar.xz" % led["period_start"]
    if not os.path.exists(os.path.join(backup_dir, name)):
        atomic_write(os.path.join(backup_dir, name), backup_archive(data_dir, led))
        git("add", "--", name)
        git("commit", "-q", "-m", "Back up RentCast responses for period %s" % led["period_start"], "--", name)
    git("push", "-q", "origin", "HEAD:refs/heads/" + branch)
    git("fetch", "-q", "origin")
    if git("cat-file", "-t", "origin/%s:%s" % (branch, name), check=False) != "blob":
        raise RunStop("backup of period %s is not on the backup remote after pushing" % led["period_start"])
    return git("rev-parse", "HEAD")


def back_up_periods(data_dir):
    """Back up every collected period that has no recorded backup."""
    if not BACKUP_DIR:
        return
    closed = closed_periods()
    for ledger in ledgers(data_dir):
        led = ledger.s
        if led.get("backed_up") or led["period_start"] not in closed or not led.get("scrape_ts"):
            continue
        if not all(os.path.exists(os.path.join(data_dir, f)) for f in raw_files(led)):
            print("can't back up period %s: its raw responses were pruned" % led["period_start"])
            continue
        led["backed_up"] = back_up(BACKUP_DIR, data_dir, led)
        ledger.save()
        print("backed up period %s at %s" % (led["period_start"], led["backed_up"][:10]))


RESTORABLE = re.compile(r"runs/\d{4}-\d{2}-\d{2}\.json|raw/\d{4}-\d{2}-\d{2}/[A-Za-z0-9_.-]+\.json")


def restore(backup_dir, data_dir):
    """Unpack every period archive into the data directory, leaving existing files alone."""
    written = skipped = 0
    for name in sorted(os.listdir(backup_dir)):
        if not name.endswith(".tar.xz"):
            continue
        with tarfile.open(os.path.join(backup_dir, name), "r:xz") as tar:
            for m in tar.getmembers():
                if not m.isfile() or not RESTORABLE.fullmatch(m.name):
                    raise RunStop("unexpected entry %r in %s" % (m.name, name))
                raw = m.name.startswith("raw/")
                dest = os.path.join(data_dir, m.name + (".gz" if raw else ""))
                if os.path.exists(dest):
                    skipped += 1
                    continue
                data = tar.extractfile(m).read()
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                atomic_write(dest, gzip.compress(data) if raw else data)
                written += 1
    print("restored %d files (%d already present); `publish` rebuilds the scrapes and site"
          % (written, skipped))


def build_fingerprint(repo):
    city = load_city()
    h = hashlib.sha256(ScrapeFilter().fingerprint.encode())
    for rel in ("cities/nyc.json", city["mask"], city["units"]):
        path = os.path.join(repo, rel)
        if os.path.exists(path):
            with open(path, "rb") as f:
                h.update(f.read())
        else:
            h.update(b"missing " + rel.encode())
    return h.hexdigest()[:16]


def refresh_market_data(data_dir, ledger):
    """Download the market data if this period hasn't; returns a failure message or None.

    The ledger's "published" mark is cleared so that new data is published.
    """
    if not market_data.configured():
        return None
    try:
        want = market_data.config_id()
        if ledger.s.get("market_data") == want:
            return None
        failures = market_data.refresh(data_dir)
    except market_data.MarketDataError as e:
        return str(e)
    if len(failures) < len(market_data.sources()):
        ledger.s.pop("published", None)
    if not failures:
        ledger.s["market_data"] = want
    ledger.save()
    return "; ".join(failures) or None


def build_and_publish(repo, data_dir, git, label):
    site = os.path.join(data_dir, "site")
    os.makedirs(site, exist_ok=True)
    stamp = os.path.join(data_dir, "site.fingerprint")
    fingerprint = build_fingerprint(repo)
    force = not os.path.exists(stamp) or open(stamp).read().strip() != fingerprint
    if force:
        print("rebuilding every month: first build, or the city settings or filter changed")
    p = subprocess.run([sys.executable, "-B", os.path.join(repo, "build_rentmap.py"),
                        os.path.join(data_dir, "v3"), site] + (["--force"] if force else []),
                       cwd=repo, env=dict(os.environ, RENTMAP_CITY="nyc"),
                       capture_output=True, text=True, timeout=3600)
    print(p.stdout.strip())
    if p.returncode:
        raise RunStop("site build failed: %s" % p.stderr.strip()[-2000:])
    atomic_write(stamp, fingerprint.encode())
    open(os.path.join(site, ".nojekyll"), "w").close()
    commit = git.publish_tree(site, "gh-pages", "Publish NYC rent map: %s" % label)
    print("gh-pages %s" % ("unchanged" if commit is None else "updated to %s" % commit[:10]))
    return commit or "unchanged"


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
        self.filter = ScrapeFilter()

    def quota(self):
        return rentcast_quota.Quota(self.quota_path)

    def run(self):
        if self.git.dirty(self.quota_rel):
            self.git.run("commit", "-q", "-m", "Commit quota changes left by an interrupted run",
                         "--", self.quota_rel)
        restart_if_updated(self.git.sync())
        refilter_scrapes(self.data_dir, self.filter)
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
            self.finish()
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
        self.finish()

    def finish(self):
        led = self.ledger.s
        failure = None
        if led["scrape_ts"]:
            failure = refresh_market_data(self.data_dir, self.ledger)
            if not led.get("published"):
                self.publish_site()
        back_up_periods(self.data_dir)
        if failure:
            raise RunStop("market data: %s" % failure)

    def publish_site(self):
        self.ledger.s["published"] = build_and_publish(
            self.git.repo, self.data_dir, self.git, "period %s" % self.ledger.s["period_start"])
        self.ledger.s["published_at"] = now_iso()
        self.ledger.save()

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

    def capacity(self):
        """Calls this run may still spend, counting any it has already reserved."""
        q = self.quota()
        return (q.state["monthly_limit"] - self.cfg["keep_free"]
                - q.used(self.today, exclude=self.res_id) - self.ledger.billed())

    def previous_total(self, name):
        runs = os.path.join(self.data_dir, "runs")
        for fn in sorted(os.listdir(runs), reverse=True):
            if not fn.endswith(".json") or fn[:-5] >= self.ledger.s["period_start"]:
                continue
            with open(os.path.join(runs, fn)) as f:
                total = json.load(f)["passes"].get(name, {}).get("total")
            if total is not None:
                return total
        return None

    def run_pass(self, p):
        st = self.ledger.s["passes"].setdefault(p["name"], {"status": "pending", "done": []})
        if st["status"] != "pending":
            return
        size = self.cfg["page_size"]
        use_all = p.get("use_all_quota", False)
        if "total" not in st:
            if 0 in self.ledger.lost_offsets(p["name"]) and self.room() < 1:
                st["status"] = "incomplete"
                return self.ledger.save()
            estimate = None if use_all else self.previous_total(p["name"])
            if estimate is not None and max(1, math.ceil(estimate / size)) > self.capacity():
                st["status"] = "skipped"
                st["estimate"] = estimate
                print("  %s: last period's %d listings need %d pages; %d calls available"
                      % (p["name"], estimate, max(1, math.ceil(estimate / size)), self.capacity()))
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
        if fresh and use_all:
            fresh = fresh[:max(0, min(len(fresh), self.capacity()))]
            if fresh and not self.ensure_reserved(self.ledger.billed() + len(fresh)):
                fresh = []
        elif fresh and not self.ensure_reserved(self.ledger.billed() + len(fresh)):
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
        write_scrape(self.data_dir, self.ledger.s, self.filter)
        self.ledger.save()

    def scrape_path(self):
        return scrape_path(self.data_dir, self.ledger.s)

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
    if not argv or argv[0] not in ("run", "status", "publish", "restore"):
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
            print("  published: %s" % s.get("published", "not yet"))
            print("  market data: %s" % ("fetched" if s.get("market_data") else
                                         "not yet" if market_data.configured() else "off"))
            print("  backed up: %s" % (s.get("backed_up") or ("not yet" if BACKUP_DIR else "backups off")))
        else:
            print("  no run yet this period")
        return
    key = os.environ.get("RENTCAST_API_KEY")
    if argv[0] == "run" and not key:
        sys.exit("RENTCAST_API_KEY is not set")
    with open(os.path.join(data_dir, "collect.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another collection run is in progress")
        try:
            if argv[0] == "restore":
                if not BACKUP_DIR:
                    raise RunStop("NYC_RENTMAP_BACKUP_DIR is not set")
                restore(BACKUP_DIR, data_dir)
            elif argv[0] == "publish":
                git = Git(repo)
                restart_if_updated(git.sync())
                refilter_scrapes(data_dir, ScrapeFilter())
                collected = [x for x in ledgers(data_dir) if x.s["period_start"] in closed_periods()]
                failure = refresh_market_data(data_dir, collected[-1]) if collected else None
                build_and_publish(repo, data_dir, git, "manual rebuild")
                if failure:
                    raise RunStop("market data: %s" % failure)
            else:
                Collector(repo, data_dir, key).run()
        except (RunStop, rentcast_quota.QuotaError) as e:
            sys.exit("run stopped: %s" % e)


if __name__ == "__main__":
    main(sys.argv[1:])
