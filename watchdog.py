"""Off-Mac watchdog for the daily-levels dashboard.

Runs on GitHub Actions, never on the Mac, so it cannot share fate with the
launchd agents it is watching. That is the whole point: on 2026-09-30 twelve
quarantined plists failed to load after a reboot and every on-Mac monitor died
with them, so the only thing that noticed was Mo opening the page himself.

Probes the PUBLISHED page rather than the Mac: if the builder is dead the
"Generated" stamp stops advancing, which is the observable symptom of the whole
class of failure (launchd not loading, VPN down, parse failing, Mac asleep).

State is committed back to the repo because Actions runs are stateless and an
in-memory set would re-alert on every run.
"""
import datetime as dt
import json
import os
import re
import urllib.request
from zoneinfo import ZoneInfo

PAGE = "https://momokuraishi-cpu.github.io/koda-daily-levels/"
STATE = "watchdog_state.json"
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
AMS = ZoneInfo("Europe/Amsterdam")

# Builder fires 21:15, 22:30, 23:45, 00:00, 06:30 Mon-Fri. A healthy weekday
# morning page is ~1h old. 14h catches a missed overnight cycle without firing
# on the legitimate weekend gap.
MAX_AGE_H = 14
RENOTIFY_H = 6          # do not re-nag about the same fault more often than this
MIN_BYTES = 40_000      # a blank/degraded build is much smaller than the ~80KB real one


def _get(url, timeout=25):
    req = urllib.request.Request(
        url, headers={"User-Agent": "koda-watchdog/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _notify(title, body, priority="high", tags="warning"):
    req = urllib.request.Request(
        f"{NTFY_URL}/{NTFY_TOPIC}",
        data=body.encode(),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        method="POST")
    urllib.request.urlopen(req, timeout=20).read()


def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=2, sort_keys=True)


def _should_notify(state, key, now):
    last = state.get("last_alert", {}).get(key)
    if not last:
        return True
    try:
        prev = dt.datetime.fromisoformat(last)
    except Exception:
        return True
    return (now - prev).total_seconds() >= RENOTIFY_H * 3600


def _mark(state, key, now):
    state.setdefault("last_alert", {})[key] = now.isoformat()


def main():
    now = dt.datetime.now(AMS)
    state = _load()
    faults = []

    # 1. Is the published page even reachable and non-degraded?
    try:
        status, html = _get(f"{PAGE}?cb={int(now.timestamp())}")
    except Exception as e:
        faults.append(("unreachable", f"Dashboard unreachable: {type(e).__name__}: {e}"))
        status, html = None, ""
    else:
        if status != 200:
            faults.append(("http", f"Dashboard returned HTTP {status}"))
        elif len(html) < MIN_BYTES:
            faults.append(("degraded",
                            f"Dashboard is only {len(html)} bytes (expected >{MIN_BYTES}). "
                            "Likely a blank or partial build."))

    # 2. Is the build stamp advancing? This is the real dead-man's-switch.
    if html:
        m = re.search(r"Generated (\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})", html)
        if not m:
            faults.append(("nostamp", "No 'Generated' timestamp found on the page."))
        else:
            built = dt.datetime(int(m.group(1)[:4]), int(m.group(1)[5:7]),
                                int(m.group(1)[8:10]), int(m.group(2)),
                                int(m.group(3)), tzinfo=AMS)
            age_h = (now - built).total_seconds() / 3600
            state["last_seen_build"] = built.isoformat()
            state["last_age_h"] = round(age_h, 2)
            # Only judge staleness on a weekday, and only once the 06:30 build
            # has had time to land. The builder does not run Sat/Sun, so the
            # weekend gap is expected, not a fault.
            weekday = now.weekday() <= 4
            past_morning = now.hour >= 8
            if weekday and past_morning and age_h > MAX_AGE_H:
                faults.append((
                    "stale",
                    f"Dashboard has not rebuilt in {age_h:.1f}h.\n"
                    f"Last build: {built:%a %d %b %H:%M} Amsterdam.\n"
                    "The Mac's launchd agents are probably not loaded. Check:\n"
                    "  launchctl list | grep -i koda\n"
                    "  xattr -l ~/Library/LaunchAgents/*.plist | grep -c quarantine"))

    state["last_run"] = now.isoformat()
    state["last_status"] = "fault" if faults else "ok"

    for key, msg in faults:
        if _should_notify(state, key, now):
            _notify(f"Dashboard watchdog: {key}", msg)
            _mark(state, key, now)
            print(f"ALERTED {key}: {msg}")
        else:
            print(f"suppressed (within {RENOTIFY_H}h): {key}")

    if not faults:
        # Clear the nag timers so a recurrence alerts immediately.
        state["last_alert"] = {}
        print(f"ok: build {state.get('last_seen_build')} age {state.get('last_age_h')}h")

    _save(state)


if __name__ == "__main__":
    main()
