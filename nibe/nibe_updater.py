#!/usr/bin/env python3
"""NIBE-only planner. Never edits the existing elpris_1h..4h files.

Python 3.11+, requests. Computes on GitHub, sends small UTC windows to Shelly.
Price optimum is for an equal-energy time window, NOT a thermal/COP model.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Stockholm")
UTC = timezone.utc
SYSTEM = "nibe-aux-v1"
FILENAME = "nibe_plan.json"
# Protect the four Gists supplied in the conversation against accidental use.
PROTECTED_GISTS = {
    "38a1294a14afc7b05553c2658576a721", "8e3e8e4683688fe5a56720c0c0787e83",
    "765b903dea2d354ad5930faddf679e20", "096847cdcd7826a86b989d744b162584",
}


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Slot:
    start: int
    end: int
    price: float


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def day_bounds(day: date) -> tuple[int, int]:
    start = datetime.combine(day, dtime(), TZ)
    end = datetime.combine(day + timedelta(days=1), dtime(), TZ)
    return int(start.timestamp()), int(end.timestamp())


def parse_iso(text: str) -> int:
    if not isinstance(text, str):
        raise PlanError("Price timestamp is not text")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.microsecond:
        raise PlanError("Price timestamp needs an explicit timezone and whole seconds")
    return int(parsed.timestamp())


def parse_prices(data, day: date) -> list[Slot]:
    """Require exact, gap-free coverage of the actual 23/24/25-hour local day."""
    if not isinstance(data, list) or not data:
        raise PlanError("Empty/invalid price list")
    slots = []
    try:
        for entry in data:
            price = entry["SEK_per_kWh"]
            if not finite_number(price):
                raise PlanError("Invalid or non-finite price")
            slots.append(Slot(parse_iso(entry["time_start"]),
                              parse_iso(entry["time_end"]), float(price)))
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError(f"Invalid price row: {exc}") from exc
    slots.sort(key=lambda slot: slot.start)
    step = slots[0].end - slots[0].start
    if step not in (900, 3600):
        raise PlanError("Only 15-minute or 60-minute price records are supported")
    cursor, day_end = day_bounds(day)
    for slot in slots:
        if slot.start != cursor or slot.end - slot.start != step:
            raise PlanError("Missing/duplicate/overlapping price interval")
        cursor = slot.end
    if cursor != day_end:
        raise PlanError("Incomplete price day or wrong date")
    return slots


def aggregate_prices(slots: list[Slot], resolution: int) -> list[Slot]:
    """UTC intervals preserve both occurrences of 02:00 on an autumn DST day."""
    native = slots[0].end - slots[0].start
    if resolution == 15 or native == 3600:
        return slots
    result = []
    for i in range(0, len(slots), 4):
        part = slots[i:i + 4]
        if len(part) != 4:
            raise PlanError("Incomplete hourly price group")
        result.append(Slot(part[0].start, part[-1].end,
                           sum(x.price for x in part) / 4))
    return result


def minute_of_day(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise PlanError("Times must use HH:MM")
    h, m = map(int, value.split(":"))
    if not (0 <= h <= 24 and 0 <= m < 60 and (h < 24 or m == 0)):
        raise PlanError("Time outside 00:00..24:00")
    return h * 60 + m


def local_window_contains(start: int, end: int, opening: str, closing: str) -> bool:
    a = datetime.fromtimestamp(start, TZ)
    b = datetime.fromtimestamp(end - 1, TZ)
    return (a.date() == b.date() and
            a.hour * 60 + a.minute >= minute_of_day(opening) and
            b.hour * 60 + b.minute < minute_of_day(closing))


def overlaps(a, b) -> bool:
    return bool(a and b and a[0] < b[1] and b[0] < a[1])


def blocked_local(start: int, end: int, intervals: list) -> bool:
    # Scan UTC quarters rather than constructing ambiguous local DST instants.
    for stamp in range(start, end, 900):
        d = datetime.fromtimestamp(stamp, TZ)
        m = d.hour * 60 + d.minute
        for opening, closing in intervals:
            if minute_of_day(opening) <= m < minute_of_day(closing):
                return True
    return False


def weather_mean_before(stamp: int, temperatures: dict[int, float]) -> float | None:
    hour = (stamp // 3600) * 3600
    values = [temperatures.get(hour - i * 3600) for i in range(1, 25)]
    if any(not finite_number(x) for x in values):
        return None
    return sum(values) / 24


def cheapest(slots: list[Slot], rule: dict, *, not_before=0,
             exclude=(), temperatures=None) -> list[int]:
    duration = rule["duration_minutes"] * 60
    best = None
    for i, first in enumerate(slots):
        end = first.start + duration
        if first.start < not_before:
            continue
        if not local_window_contains(first.start, end, rule["start"], rule["end"]):
            continue
        if any(overlaps([first.start, end], other) for other in exclude):
            continue
        if blocked_local(first.start, end, rule.get("avoid", [])):
            continue
        if temperatures is not None:
            average = weather_mean_before(first.start, temperatures)
            if average is None or average >= rule["outdoor_mean_below_c"]:
                continue
        energy_price = 0.0
        cursor = first.start
        for slot in slots[i:]:
            if slot.start != cursor or slot.end > end:
                break
            energy_price += slot.price * (slot.end - slot.start)
            cursor = slot.end
            if cursor == end:
                # Near-identical prices: choose later window, nearer consumption.
                rank = (round(energy_price / duration, 8), -first.start)
                if best is None or rank < best[0]:
                    best = (rank, [first.start, end])
                break
    return [] if best is None else best[1]


def validate_config(cfg):
    if cfg.get("area") not in ("SE1", "SE2", "SE3", "SE4"):
        raise PlanError("Invalid area")
    resolution = cfg.get("selection_resolution_minutes")
    if type(resolution) is not int or resolution not in (15, 60):
        raise PlanError("selection_resolution_minutes must be 15 or 60")
    for key, maximum in (("water", 240), ("heat", 360)):
        rule = cfg.get(key)
        if not isinstance(rule, dict) or type(rule.get("enabled")) is not bool:
            raise PlanError(f"Invalid {key} config")
        length = rule.get("duration_minutes")
        if (type(length) is not int or not resolution <= length <= maximum
                or length % resolution):
            raise PlanError(f"{key}: duration must fit the selected price resolution")
        if minute_of_day(rule["start"]) >= minute_of_day(rule["end"]):
            raise PlanError(f"{key}: use a window inside one local day")
    for opening, closing in cfg["heat"].get("avoid", []):
        if minute_of_day(opening) >= minute_of_day(closing):
            raise PlanError("Invalid heat exclusion window")
    heat = cfg["heat"]
    if not finite_number(heat.get("outdoor_mean_below_c")):
        raise PlanError("Missing heat temperature threshold")
    if heat["enabled"]:
        for field, limit in (("latitude", 90), ("longitude", 180)):
            if not finite_number(heat.get(field)) or abs(heat[field]) > limit:
                raise PlanError(f"Set {field} before enabling heat")


def request(method: str, url: str, **kwargs):
    kwargs.setdefault("timeout", 20)
    for attempt in range(3):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code < 500 and response.status_code != 429:
                return response
        except requests.RequestException:
            pass
        if attempt < 2:
            time.sleep(2 ** (attempt + 1))
    raise PlanError("Network request failed after three attempts (no token logged)")


def fetch_prices(day, area):
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{day.year}/{day:%m-%d}_{area}.json"
    response = request("GET", url)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise PlanError(f"Price API status {response.status_code}")
    return parse_prices(response.json(), day)


def fetch_weather(cfg) -> dict[int, float]:
    heat = cfg["heat"]
    if not heat["enabled"]:
        return {}
    try:
        res = request("GET", "https://api.open-meteo.com/v1/forecast", params={
            "latitude": heat["latitude"], "longitude": heat["longitude"],
            "hourly": "temperature_2m", "timeformat": "unixtime", "timezone": "UTC",
            "past_days": 2, "forecast_days": 3,
        })
        if res.status_code != 200:
            raise PlanError(f"Weather API status {res.status_code}")
        hourly = res.json()["hourly"]
        times, values = hourly["time"], hourly["temperature_2m"]
        if len(times) != len(values):
            raise PlanError("Weather arrays differ in length")
        return {int(t): v for t, v in zip(times, values)
                if type(t) is int and finite_number(v)}
    except (PlanError, KeyError, ValueError, TypeError):
        print("WARNING: weather unavailable; no additional house heating.", file=sys.stderr)
        return {}


def valid_day(entry, day) -> bool:
    if not isinstance(entry, dict):
        return False
    a, b = day_bounds(day)
    if entry.get("date") != day.isoformat() or entry.get("start") != a or entry.get("end") != b:
        return False
    for key, maximum in (("heat", 21600), ("water", 14400)):
        w = entry.get(key)
        if not isinstance(w, list) or len(w) not in (0, 2):
            return False
        if w and (any(type(v) is not int for v in w) or
                  not a <= w[0] < w[1] <= b or w[1] - w[0] > maximum):
            return False
    return not overlaps(entry["heat"], entry["water"])


def previous_day(old, day, area):
    if not isinstance(old, dict) or old.get("system") != SYSTEM or old.get("area") != area:
        return None
    for entry in old.get("days", []):
        if valid_day(entry, day):
            return dict(entry)
    return None


def build_day(day, cfg, slots, weather, now, previous=None):
    """Freeze today's published periods; do not add a second boost after re-planning."""
    a, b = day_bounds(day)
    current_day = day == now.astimezone(TZ).date()
    if previous is not None and current_day:
        result = dict(previous)
        if not cfg["water"]["enabled"]:
            result["water"] = []
        hw = result["heat"]
        average = weather_mean_before(hw[0], weather) if hw else None
        if (not cfg["heat"]["enabled"] or average is None or
                average >= cfg["heat"]["outdoor_mean_below_c"]):
            result["heat"] = []
        return result
    if slots is None:
        return previous
    slots = aggregate_prices(slots, cfg["selection_resolution_minutes"])
    floor = int(now.timestamp()) if current_day else a
    water = cheapest(slots, cfg["water"], not_before=floor) if cfg["water"]["enabled"] else []
    heat = cheapest(slots, cfg["heat"], not_before=floor, exclude=[water], temperatures=weather) \
        if cfg["heat"]["enabled"] else []
    return {"date": day.isoformat(), "start": a, "end": b, "heat": heat, "water": water}


def gist_headers(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def load_old(gist_id, token):
    res = request("GET", f"https://api.github.com/gists/{gist_id}", headers=gist_headers(token))
    if res.status_code != 200:
        raise PlanError(f"Gist read failed: HTTP {res.status_code}. Check ID/token permission.")
    file = res.json().get("files", {}).get(FILENAME)
    if not file:
        return {}
    if file.get("truncated"):
        raise PlanError("NIBE plan unexpectedly truncated; refusing oversized file")
    try:
        return json.loads(file["content"])
    except (KeyError, ValueError):
        return {}


def compact(payload):
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--publish", action="store_true", help="Write ONLY to NIBE_GIST_ID")
    parser.add_argument("--out", default="nibe_plan.json")
    args = parser.parse_args()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    validate_config(cfg)
    now = datetime.now(UTC)
    gist_id, token, old = "", "", {}
    if args.publish:
        gist_id, token = os.getenv("NIBE_GIST_ID", "").strip(), os.getenv("NIBE_GIST_TOKEN", "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{20,40}", gist_id) or gist_id in PROTECTED_GISTS:
            raise PlanError("Create a separate NIBE Gist and set NIBE_GIST_ID")
        if not token:
            raise PlanError("Missing NIBE_GIST_TOKEN; use a Gist-capable PAT, not the default token")
        old = load_old(gist_id, token)
    weather = fetch_weather(cfg)
    today = now.astimezone(TZ).date()
    days, failed = [], False
    for day in (today, today + timedelta(days=1)):
        previous = previous_day(old, day, cfg["area"])
        try:
            prices = fetch_prices(day, cfg["area"])
            if prices is None:
                print(f"Prices not published for {day}; preserving any valid existing plan.")
                if day == today or now.astimezone(TZ).hour >= 20:
                    failed = True
        except (PlanError, ValueError) as exc:
            print(f"WARNING {day}: {exc}", file=sys.stderr)
            prices, failed = None, True
        entry = build_day(day, cfg, prices, weather, now, previous)
        if entry is not None:
            # Even on an API outage, disable optional heating without weather.
            if not cfg["heat"]["enabled"] or not weather:
                entry["heat"] = []
            if not cfg["water"]["enabled"]:
                entry["water"] = []
            days.append(entry)
    if not days:
        raise PlanError("No usable day. Existing Gist was not overwritten.")
    payload = {"system": SYSTEM, "area": cfg["area"], "generated": int(now.timestamp()), "days": days}
    text = compact(payload)
    if len(text.encode("ascii")) > 900:
        raise PlanError("Plan exceeds the 900-byte device budget")
    Path(args.out).write_text(text + "\n", encoding="ascii")
    for entry in days:
        print(entry["date"])
        for key in ("heat", "water"):
            w = entry[key]
            times = "OFF" if not w else " -> ".join(datetime.fromtimestamp(t, TZ).isoformat() for t in w)
            print(f"  {key}: {times}")
    print(f"Payload: {len(text)} bytes. Resolution: {cfg['selection_resolution_minutes']} minutes.")
    if args.publish:
        res = request("PATCH", f"https://api.github.com/gists/{gist_id}", headers=gist_headers(token),
                      json={"files": {FILENAME: {"content": text}}})
        if res.status_code != 200:
            raise PlanError(f"Gist update failed: HTTP {res.status_code}")
        print("Published nibe_plan.json. Existing 1h/2h/3h/4h Gists untouched.")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (PlanError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
