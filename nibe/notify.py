"""Send a plain-language NIBE plan summary to the phone through ntfy.sh.

Reads the plan written by nibe_updater.py (default /tmp/nibe_plan.json).
Needs env NTFY_TOPIC. Without it, prints the text and exits 0.
Sends only the plan times (no tokens, no gist id).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Stockholm")
DAYS = ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"]
MONTHS = ["januari", "februari", "mars", "april", "maj", "juni", "juli",
          "augusti", "september", "oktober", "november", "december"]


def hhmm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def windows(w):
    if len(w) != 2:
        return "ingen"
    return f"{hhmm(w[0])}–{hhmm(w[1])} ({(w[1] - w[0]) // 60} min)"


def describe(day):
    d = datetime.strptime(day["date"], "%Y-%m-%d")
    name = f"{DAYS[d.weekday()]} {d.day} {MONTHS[d.month - 1]}"
    return (f"{name}\n"
            f"Varmvatten (SG Ready B): {windows(day['water'])}\n"
            f"Extra husvärme: {windows(day['heat'])}")


def build(plan, now):
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    by_date = {d["date"]: d for d in plan.get("days", [])}
    parts = []
    if tomorrow in by_date:
        parts.append(describe(by_date[tomorrow]))
    else:
        parts.append("Ingen plan för i morgon ännu. Skriptet gör inget tills planen finns.")
    parts.append("Lyx är alltid manuell. Planen visar vad skriptet får göra, inte vad som hänt.")
    return "\n\n".join(parts)


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/nibe_plan.json")
    plan = json.loads(path.read_text())
    text = build(plan, datetime.now(TZ))
    print(text)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC not set: nothing sent.")
        return
    req = urllib.request.Request(
        "https://ntfy.sh/" + topic,
        data=text.encode("utf-8"),
        headers={"Title": "NIBE plan", "Tags": "thermometer"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        print("ntfy status", r.status)


if __name__ == "__main__":
    main()
