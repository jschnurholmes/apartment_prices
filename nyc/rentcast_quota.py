"""Monthly RentCast quota tracker for the recurring collection run.

RentCast bills "each month, on the same day of the month that you first
activated your current API plan", and the quota resets at the end of each
billing period (or on a plan change).  RentCast has no hard cap, so this file
is the only thing preventing overage.

The reset happens at the activation time of day, which this file doesn't know,
so calls dated on a reset day count toward both adjacent periods.

State lives in rentcast_quota.json:
  monthly_limit   requests allowed per billing period (Developer plan: 50)
  billing_anchor  any date a billing period starts on (YYYY-MM-DD), e.g. the
                  plan activation date; calls are refused until it is set
  external_usage  dated entries for requests made outside the monthly run
                  (development calls, manual calls)
  calls           requests made by the monthly run, recorded before sending

Only HTTP 200 responses are billed; a call that never got a response counts
as billed, since it may have been.

CLI:
  python3 -I rentcast_quota.py status [--date YYYY-MM-DD]
  python3 -I rentcast_quota.py set-anchor YYYY-MM-DD
  python3 -I rentcast_quota.py set-limit N
  python3 -I rentcast_quota.py set-usage-before-run N [--date YYYY-MM-DD] [--note TEXT]
  python3 -I rentcast_quota.py add-usage N [--date YYYY-MM-DD] [--note TEXT]

set-usage-before-run makes the period's external usage exactly N (e.g. the
count shown on the RentCast dashboard minus this run's calls); add-usage adds N.
"""

import calendar
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "rentcast_quota.json")


class QuotaError(Exception):
    pass


def _parse_date(s):
    return datetime.date.fromisoformat(s)


def _today():
    return datetime.datetime.now(datetime.timezone.utc).date()


def _clamped(year, month, day):
    return datetime.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _add_months(year, month, n):
    m = year * 12 + (month - 1) + n
    return m // 12, m % 12 + 1


class Quota:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        if os.path.exists(path):
            with open(path) as f:
                self.state = json.load(f)
        else:
            self.state = {"monthly_limit": 50, "billing_anchor": None,
                          "external_usage": [], "calls": []}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def period(self, when=None):
        """(start, end) of the billing period containing `when`; end is exclusive."""
        anchor = self.state.get("billing_anchor")
        if not anchor:
            raise QuotaError("billing_anchor is not set; run: rentcast_quota.py set-anchor YYYY-MM-DD "
                             "(the RentCast plan activation date, or any later period start)")
        day = _parse_date(anchor).day
        when = when or _today()
        start = _clamped(when.year, when.month, day)
        if start > when:
            y, m = _add_months(when.year, when.month, -1)
            start = _clamped(y, m, day)
        y, m = _add_months(start.year, start.month, 1)
        return start, _clamped(y, m, day)

    def _in(self, date_str, start, end):
        return start <= _parse_date(date_str[:10]) <= end

    def external_used(self, when=None):
        start, end = self.period(when)
        return sum(e["calls"] for e in self.state["external_usage"] if self._in(e["date"], start, end))

    def run_used(self, when=None):
        start, end = self.period(when)
        return sum(1 for c in self.state["calls"]
                   if self._in(c["started"], start, end) and c.get("billed", True))

    def used(self, when=None):
        return self.external_used(when) + self.run_used(when)

    def remaining(self, when=None):
        return self.state["monthly_limit"] - self.used(when)

    def begin_call(self, params, label=""):
        """Record a call before sending it; raises QuotaError if none remain."""
        if self.remaining() < 1:
            start, end = self.period()
            raise QuotaError("monthly quota exhausted: %d of %d used in %s to %s"
                             % (self.used(), self.state["monthly_limit"], start, end))
        entry = {"id": len(self.state["calls"]) + 1,
                 "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "label": label, "params": params, "result": "pending", "billed": True}
        self.state["calls"].append(entry)
        self.save()
        return entry["id"]

    def finish_call(self, call_id, status, **info):
        entry = self.state["calls"][call_id - 1]
        entry["result"] = status
        entry["billed"] = status == 200
        entry.update(info)
        self.save()

    def set_external(self, n, when=None, note=""):
        when = when or _today()
        start, end = self.period(when)
        self.state["external_usage"] = [e for e in self.state["external_usage"]
                                        if not self._in(e["date"], start, end)]
        self.state["external_usage"].append({"date": when.isoformat(), "calls": n,
                                             "note": note or "set-usage-before-run"})
        self.save()

    def add_external(self, n, when=None, note=""):
        when = when or _today()
        self.state["external_usage"].append({"date": when.isoformat(), "calls": n,
                                             "note": note or "add-usage"})
        self.save()


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        value = args[i + 1]
        del args[i:i + 2]
        return value
    return default


def main(argv):
    args = list(argv)
    when = _opt(args, "--date")
    when = _parse_date(when) if when else None
    note = _opt(args, "--note", "")
    if not args:
        sys.exit(__doc__)
    q = Quota()
    cmd = args[0]
    try:
        if cmd == "status":
            print("monthly limit: %d | billing anchor: %s"
                  % (q.state["monthly_limit"], q.state.get("billing_anchor")))
            start, end = q.period(when)
            print("period %s to %s (resets %s)" % (start, end - datetime.timedelta(days=1), end))
            print("  used before/outside run: %d" % q.external_used(when))
            print("  used by monthly run:     %d" % q.run_used(when))
            print("  remaining:               %d" % q.remaining(when))
        elif cmd == "set-anchor":
            _parse_date(args[1])
            q.state["billing_anchor"] = args[1]
            q.save()
            print("billing anchor set to", args[1])
        elif cmd == "set-limit":
            q.state["monthly_limit"] = int(args[1])
            q.save()
            print("monthly limit set to", args[1])
        elif cmd == "set-usage-before-run":
            q.set_external(int(args[1]), when, note)
            print("external usage for the period set to", args[1])
        elif cmd == "add-usage":
            q.add_external(int(args[1]), when, note)
            print("added", args[1], "external calls")
        else:
            sys.exit(__doc__)
    except QuotaError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main(sys.argv[1:])
