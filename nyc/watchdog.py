"""Check that the Pi collected and published the NYC rent map this billing period.

Run daily by .github/workflows/nyc-watchdog.yml. From GRACE_DAYS after the
billing period starts, it exits 1 (failing the workflow, so GitHub emails its
owner) unless the period's reservation in rentcast_quota.json is closed and
origin/gh-pages has a publish commit made after the reservation closed.
Fetch gh-pages into origin/gh-pages before running it.

usage: python3 -I nyc/watchdog.py [--today YYYY-MM-DD]
"""

import datetime
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rentcast_quota

GRACE_DAYS = 2
PUBLISH_SUBJECT = "Publish NYC rent map:"


def last_publish(repo, ref="origin/gh-pages"):
    p = subprocess.run(["git", "-C", repo, "log", "--format=%cI %s", ref],
                       capture_output=True, text=True)
    if p.returncode:
        return None
    for line in p.stdout.splitlines():
        when, _, subject = line.partition(" ")
        if subject.startswith(PUBLISH_SUBJECT):
            return datetime.datetime.fromisoformat(when)
    return None


def check(today):
    q = rentcast_quota.Quota()
    start, _ = q.period(today)
    due = start + datetime.timedelta(days=GRACE_DAYS)
    if today < due:
        return True, "period %s: not checked until %s" % (start, due)
    r = q.reservation("run-" + start.isoformat())
    if not r:
        return False, "period %s: no collection has started" % start
    if r["status"] != "closed":
        return False, "period %s: collection started but not finished (%d of %d calls spent)" % (
            start, r["spent"], r["reserved"])
    closed = datetime.datetime.fromisoformat(r["closed"])
    published = last_publish(os.path.dirname(HERE))
    if published is None or published < closed:
        return False, "period %s: collected (%d calls) but not published since %s" % (
            start, r["spent"], r["closed"])
    return True, "period %s: collected (%d calls) and published %s" % (
        start, r["spent"], published.isoformat())


def main(argv):
    today = rentcast_quota._today()
    if argv[:1] == ["--today"]:
        today = datetime.date.fromisoformat(argv[1])
    ok, message = check(today)
    print(("OK: " if ok else "FAIL: ") + message)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
