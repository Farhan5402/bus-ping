#!/usr/bin/env python3
"""Notify via ntfy.sh when a TripShot shuttle reaches two configured stops.

Stdlib only. Polls the public TripShot live endpoint during the commute window.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _req(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing required env var {name}")
    return v


ROUTE_ID = _req("ROUTE_ID")
URL = ("https://uta.tripshot.com/v2/p/shared/route/{rid}/live"
       "?day={day}&includePreviousDayRidesSpanningMidnight=true")
TZ = ZoneInfo("America/Chicago")

# Stop IDs and labels come from the environment so the repo holds no personal info.
EARLY_STOP = _req("EARLY_STOP_ID")      # "get ready" trigger
LEAVE_STOP = _req("LEAVE_STOP_ID")      # "leave now" trigger
BOARD_STOP = _req("BOARD_STOP_ID")      # where you board (ETA shown in message)
EARLY_LABEL = os.environ.get("EARLY_LABEL") or "early stop"
LEAVE_LABEL = os.environ.get("LEAVE_LABEL") or "trigger stop"
BOARD_LABEL = os.environ.get("BOARD_LABEL") or "your stop"

WINDOW_START = (7, 0)
WINDOW_END = (10, 0)
POLL_SECONDS = 30
MAX_ARRIVAL_AGE = timedelta(minutes=4)   # ignore older arrivals (restart, old laps)
MAX_EARLY_WAIT = timedelta(minutes=60)   # how long before the window we are willing to sleep
ARRIVED = ("Present", "Departed")


def parse_ts(s):
    """TripShot timestamps sometimes carry 9+ fractional digits; trim to 6."""
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".")
        s = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def find_events(data, now):
    """Return (key, kind, ride_name, board_eta) for fresh arrivals at the two trigger stops."""
    out = []
    groups = (data.get("InternalRouteLiveData") or {}).get("inexactLiveData") or []
    for g in groups:
        for lr in g.get("liveRides") or []:
            ride = lr.get("ride") or {}
            if "Active" not in (ride.get("state") or {}):
                continue
            ride_id = ride.get("rideId")
            name = ride.get("vehicleName") or "Bus"
            entries = [next(iter(s.items())) for s in ride.get("stopStatus") or []]
            for i, (state, v) in enumerate(entries):
                if state not in ARRIVED or v.get("stopId") not in (EARLY_STOP, LEAVE_STOP):
                    continue
                arr = v.get("arrivalTime")
                if not arr:
                    continue
                age = now - parse_ts(arr)
                if age > MAX_ARRIVAL_AGE or age < -timedelta(minutes=1):
                    continue  # stale, or from the future (replay only)
                kind = "early" if v["stopId"] == EARLY_STOP else "leave"
                eta = None
                if i + 1 < len(entries) and entries[i + 1][1].get("stopId") == BOARD_STOP:
                    nxt = entries[i + 1][1].get("expectedArrivalTime")
                    if nxt:
                        eta = parse_ts(nxt)
                out.append(((ride_id, i, kind), kind, name, eta))
    return out


def build_message(kind, name, eta, now):
    mins = ""
    if eta:
        m = max(0, round((eta - now).total_seconds() / 60))
        mins = f" {BOARD_LABEL} ETA ~{m} min."
    if kind == "early":
        return "Bus: get ready", f"{name} reached {EARLY_LABEL}.{mins}", "default"
    return "Bus: LEAVE NOW", f"{name} reached {LEAVE_LABEL}.{mins}", "urgent"


def send(topic, title, body, priority, dry_run):
    if dry_run:
        print(f"[DRY RUN] {title} | {body} | priority={priority}")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode(),
        headers={"Title": title, "Priority": priority, "Tags": "bus"}, method="POST")
    urllib.request.urlopen(req, timeout=15).read()


def fetch(now_local):
    url = URL.format(rid=ROUTE_ID, day=now_local.strftime("%Y-%m-%d"))
    req = urllib.request.Request(url, headers={"User-Agent": "personal-bus-alert/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def window_bounds(now_local):
    d = now_local.date()
    return (datetime(d.year, d.month, d.day, *WINDOW_START, tzinfo=TZ),
            datetime(d.year, d.month, d.day, *WINDOW_END, tzinfo=TZ))


def run(topic, dry_run):
    seen = set()
    fails = 0
    warned = False
    while True:
        now_local = datetime.now(TZ)
        start, end = window_bounds(now_local)
        if now_local.weekday() >= 5 or now_local >= end:
            print("Outside service window/day, exiting.")
            return
        if now_local < start:
            if start - now_local > MAX_EARLY_WAIT:
                print("Too early, exiting.")
                return
            time.sleep(min(30, (start - now_local).total_seconds()))
            continue
        try:
            data = fetch(now_local)
            fails = 0
            now = datetime.now(timezone.utc)
            for key, kind, name, eta in find_events(data, now):
                if key in seen:
                    continue
                send(topic, *build_message(kind, name, eta, now), dry_run)
                seen.add(key)
        except Exception as e:  # network/parse problems: keep going
            fails += 1
            print(f"poll failed ({fails}): {e!r}", file=sys.stderr)
            if fails * POLL_SECONDS >= 300 and not warned:
                warned = True
                try:
                    send(topic, "Bus alert: polling failing",
                         "5+ minutes of failed polls; alerts may be missing.", "high", dry_run)
                except Exception:
                    pass
        time.sleep(POLL_SECONDS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending")
    ap.add_argument("--replay", help="JSON file to process once instead of polling")
    ap.add_argument("--now", help="ISO UTC time to use with --replay")
    a = ap.parse_args()
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic and not (a.dry_run or a.replay):
        sys.exit("Set NTFY_TOPIC")
    if a.replay:
        now = parse_ts(a.now) if a.now else datetime.now(timezone.utc)
        data = json.load(open(a.replay))
        for _, kind, name, eta in find_events(data, now):
            send(topic, *build_message(kind, name, eta, now), True)
        return
    run(topic, a.dry_run)


if __name__ == "__main__":
    main()
