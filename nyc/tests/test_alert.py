import collections
import importlib.machinery
import importlib.util
import io
import json
import os
import types
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("alert", os.path.join(HERE, "..", "deploy", "nyc-rentmap-alert"))
alert = importlib.util.module_from_spec(importlib.util.spec_from_loader("alert", loader))
loader.exec_module(alert)

Done = collections.namedtuple("Done", "returncode stdout stderr")
TOKEN = "123456:SECRET-token"
passed = failed = 0


def check(name, ok):
    global passed, failed
    print(("PASS " if ok else "FAIL ") + name)
    if ok:
        passed += 1
    else:
        failed += 1


class Host:
    def __init__(self, unit_failed, journal="docker: run stopped: git fetch failed\nexit 1"):
        self.unit_failed, self.journal, self.calls = unit_failed, journal, []

    def run(self, args, **kw):
        self.calls.append(args)
        if args[0] == "systemctl":
            return Done(0 if self.unit_failed else 1, "", "")
        return Done(0, self.journal, "")


def use(host):
    alert.subprocess = types.SimpleNamespace(run=host.run)


def telegram(sent, reply=None, error=None):
    def urlopen(url, data, timeout):
        sent.append((url, urllib.parse.parse_qs(data.decode())))
        if error:
            raise error
        return io.BytesIO(json.dumps(reply).encode())
    alert.urlopen = urlopen


def exit_message(fn):
    try:
        fn()
    except SystemExit as e:
        return str(e)
    return None


h = Host(True)
use(h)
text = alert.compose({"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_STATUS": "1",
                      "MONITOR_INVOCATION_ID": "abc123"})
check("failed run: names the result and status",
      text.startswith("NYC rent map: the daily run failed (exit-code, status 1)"))
check("failed run: reads that run's own log lines", ["journalctl", "--no-pager", "-o", "cat", "-n", "15",
                                                     "_SYSTEMD_INVOCATION_ID=abc123"] in h.calls)
check("failed run: includes the log lines", text.endswith("git fetch failed\nexit 1"))

h = Host(True)
use(h)
text = alert.compose({})
check("older systemd (no MONITOR_ variables): still reports the failure",
      text.startswith("NYC rent map: the daily run failed\n"))
check("older systemd: falls back to the service's latest log lines",
      ["journalctl", "--no-pager", "-o", "cat", "-n", "15", "-u", "nyc-rentmap.service"] in h.calls)

use(Host(False))
check("started by hand with no failure: sends a test message",
      alert.compose({}).startswith("NYC rent map: alert test."))

use(Host(True, journal="x" * 5000 + "END"))
text = alert.compose({})
tail = text.split("Last log lines:\n", 1)[1]
check("long logs trimmed to the newest 3500 characters", len(tail) == 3500 and tail.endswith("END"))

sent = []
telegram(sent, reply={"ok": True})
alert.send(TOKEN, "42", "hello")
check("sends to the bot's sendMessage with chat_id and text",
      sent == [("https://api.telegram.org/bot%s/sendMessage" % TOKEN, {"chat_id": ["42"], "text": ["hello"]})])

telegram([], reply={"ok": False, "description": "Bad Request: chat not found"})
msg = exit_message(lambda: alert.send(TOKEN, "42", "hello"))
check("Telegram refusal exits with its reason, without the token",
      msg == "Telegram refused the message: Bad Request: chat not found")

err = urllib.error.HTTPError("https://api.telegram.org/bot%s/sendMessage" % TOKEN, 401, "Unauthorized", {},
                             io.BytesIO(b'{"ok": false, "description": "Unauthorized"}'))
telegram([], error=err)
msg = exit_message(lambda: alert.send(TOKEN, "42", "hello"))
check("HTTP error exits with Telegram's reason, without the token",
      msg == "Telegram refused the message: Unauthorized")

telegram([], error=urllib.error.URLError("Name or service not known"))
msg = exit_message(lambda: alert.send(TOKEN, "42", "hello"))
check("no network exits with a clear message, without the token",
      msg == "couldn't reach Telegram: Name or service not known")

saved = dict(os.environ)
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
os.environ.pop("TELEGRAM_CHAT_ID", None)
msg = exit_message(alert.main)
os.environ.clear()
os.environ.update(saved)
check("missing settings exit with a clear message", msg == "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")

print("alert tests done: %d passed, %d failed" % (passed, failed))
