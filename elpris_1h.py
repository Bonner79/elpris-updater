#!/usr/bin/env python3
"""
Elpris 1H Updater

Hämtar spotpriser från elprisetjustnu.se, räknar ut billigaste sammanhängande
1-timmarsblocket och skriver planen till en GitHub Gist som Shelly läser 00:15.

Skillnad mot tidigare version:
  * 404 från pris-API:et betyder "inte publicerat ännu", inte fel. En körning
    före publiceringen avslutas med exit 0 i stället för att krascha.
  * Glider körningen över midnatt planeras INNEVARANDE dygn i stället för
    dagen efter morgondagen (vars priser inte finns och gav exit 1).
  * En redan publicerad plan för i morgon skrivs inte över av en plan för i dag.
  * Retry med backoff på nätverksfel och 5xx.
  * Exit 1 bara vid verkliga fel: saknade secrets, ogiltig token, sen
    publicering efter ALERT_HOUR, eller trasig prisdata.

Payloadens fält är oförändrade, så Shelly-scriptet behöver inte ändras.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# --- Konfiguration (kan överstyras med miljövariabler) ----------------------

AREA = os.environ.get("AREA", "SE3")
BLOCK_SIZE = int(os.environ.get("BLOCK_SIZE", "1"))
GIST_FILENAME = os.environ.get("GIST_FILENAME", "elpris_1h.json")

TZ = ZoneInfo("Europe/Stockholm")

# Lokal timme då morgondagens priser normalt är publicerade (dagen-före-auktionen
# publiceras ca 13:00 CET). Saknas de EFTER den här timmen är det ett verkligt
# fel värt ett rött kryss. Före den är det bara "inte klart ännu".
# Sätt den strax under lokal timme för dygnets SISTA schemalagda försök.
ALERT_HOUR = int(os.environ.get("ALERT_HOUR", "20"))

HTTP_RETRIES = 3
HTTP_BACKOFF = 5  # sekunder, multipliceras med försöksnumret
USER_AGENT = "elpris-updater (github.com/Bonner79/elpris-updater)"


class PricesNotPublished(Exception):
    """Prisdata för det begärda dygnet finns inte, eller är ofullständig."""


class FatalError(Exception):
    """Fel som ska ge exit 1 med ett läsbart meddelande."""


# --- HTTP -------------------------------------------------------------------

def http_request(method: str, url: str, **kwargs) -> requests.Response:
    """
    Gör ett anrop med retry på nätverksfel och 5xx.
    Returnerar svaret även för 4xx – anroparen avgör vad en 404 betyder.
    """
    kwargs.setdefault("timeout", 20)
    headers = {"User-Agent": USER_AGENT}
    headers.update(kwargs.pop("headers", None) or {})

    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            res = requests.request(method, url, headers=headers, **kwargs)
        except requests.RequestException as exc:
            last_error = exc
        else:
            if res.status_code < 500:
                return res
            last_error = requests.HTTPError(
                f"{res.status_code} {res.reason} från {url}", response=res
            )

        if attempt < HTTP_RETRIES:
            wait = HTTP_BACKOFF * attempt
            print(f"⚠️  Försök {attempt}/{HTTP_RETRIES} misslyckades ({last_error}). "
                  f"Väntar {wait}s.")
            time.sleep(wait)

    raise FatalError(f"Gav upp efter {HTTP_RETRIES} försök mot {url}: {last_error}")


# --- Prisdata ---------------------------------------------------------------

def fetch_hourly_prices(day: date) -> dict[int, float]:
    """
    Hämtar elprisdata för ett dygn och returnerar timpriser (SEK/kWh).
    Stödjer både 24 datapunkter (1/timme) och 96 (kvartsvis -> timmedel).
    Klarar DST-dygn med 23 eller 25 timmar.

    Kastar PricesNotPublished om dygnet inte finns eller är ofullständigt.
    """
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{day.year}/{day:%m-%d}_{AREA}.json"
    res = http_request("GET", url)

    if res.status_code == 404:
        raise PricesNotPublished(f"ingen prisdata publicerad för {day} ({AREA})")
    if res.status_code != 200:
        raise FatalError(f"Oväntat svar {res.status_code} från {url}: {res.text[:200]}")

    try:
        data = res.json()
    except ValueError as exc:
        raise FatalError(f"Kunde inte tolka svaret från {url} som JSON: {exc}") from exc

    if not data:
        raise PricesNotPublished(f"tom prisdata för {day} ({AREA})")

    buckets: dict[int, list[float]] = {}
    for entry in data:
        try:
            hour = int(entry["time_start"][11:13])
            buckets.setdefault(hour, []).append(float(entry["SEK_per_kWh"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise FatalError(f"Oväntat format i prisdatan för {day}: {entry!r} ({exc})") from exc

    # 23 timmar = DST-dygnet i mars. Färre än så är ofullständig publicering.
    if len(buckets) < 23:
        raise PricesNotPublished(
            f"ofullständig prisdata för {day}: {len(buckets)} timmar {sorted(buckets)}"
        )

    return {hour: sum(values) / len(values) for hour, values in buckets.items()}


def find_cheapest_consecutive_block(
    hour_prices: dict[int, float], block_size: int
) -> tuple[list[int], float]:
    """
    Returnerar billigaste sammanhängande blocket och dess summa.
    Tie-breaker: tidigaste blocket vid lika summa.
    """
    hours = sorted(hour_prices)
    if len(hours) < block_size:
        raise FatalError(f"För få timmar för block_size={block_size}: {hours}")

    best_idx = None
    best_sum = float("inf")

    for i in range(len(hours) - block_size + 1):
        window = hours[i:i + block_size]
        if window[-1] - window[0] != block_size - 1:
            continue  # inte sammanhängande (t.ex. DST-hål)
        total = sum(hour_prices[h] for h in window)
        if total < best_sum:
            best_sum = total
            best_idx = i

    if best_idx is None:
        raise FatalError(f"Hittade inget sammanhängande block om {block_size} h i {hours}")

    return hours[best_idx:best_idx + block_size], best_sum


def pick_target(now_local: datetime) -> tuple[date, dict[int, float]]:
    """
    Väljer vilket dygn planen ska gälla och hämtar dess priser.

    Normalfallet är morgondagen (day-ahead). Finns den inte publicerad ännu:
      * efter ALERT_HOUR  -> verkligt fel, publiceringen är sen
      * före ALERT_HOUR   -> körningen ligger före publiceringen. Har den
                             glidit över midnatt är innevarande dygn det
                             relevanta, så vi planerar för det i stället.
    """
    today = now_local.date()
    tomorrow = today + timedelta(days=1)

    try:
        return tomorrow, fetch_hourly_prices(tomorrow)
    except PricesNotPublished as exc:
        print(f"ℹ️  Morgondagens priser saknas: {exc}")

    if now_local.hour >= ALERT_HOUR:
        raise FatalError(
            f"Morgondagens priser ({tomorrow}) saknas kl {now_local:%H:%M} lokal tid. "
            f"Publiceringen är sen eller API:et har ändrats – ingen plan skriven."
        )

    print(f"ℹ️  Körningen ligger före publiceringen (kl {now_local:%H:%M}). "
          f"Planerar för innevarande dygn {today} i stället.")
    try:
        return today, fetch_hourly_prices(today)
    except PricesNotPublished as exc:
        raise FatalError(f"Även innevarande dygn saknar prisdata: {exc}") from exc


# --- Gist -------------------------------------------------------------------

def read_existing_plan(gist: dict, filename: str) -> dict | None:
    """Läser nuvarande JSON-innehåll för filen i gisten, eller None."""
    entry = (gist.get("files") or {}).get(filename)
    if not entry:
        return None

    content = entry.get("content")
    if entry.get("truncated") and entry.get("raw_url"):
        content = http_request("GET", entry["raw_url"]).text

    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def load_gist(gist_url: str, headers: dict) -> dict:
    res = http_request("GET", gist_url, headers=headers)

    if res.status_code in (401, 403):
        raise FatalError(
            f"Gist-API:et nekade åtkomst ({res.status_code}). Token är utgången, "
            f"återkallad eller saknar scopet 'gist'."
        )
    if res.status_code == 404:
        raise FatalError(
            "Gisten hittades inte (404). GIST_ID är fel, gisten borttagen, "
            "eller token saknar åtkomst till den."
        )
    if res.status_code != 200:
        raise FatalError(f"Oväntat svar {res.status_code} vid läsning av gisten: {res.text[:200]}")

    return res.json()


def patch_gist(gist_url: str, headers: dict, filename: str, payload: dict) -> None:
    body = {"files": {filename: {"content": json.dumps(payload, ensure_ascii=False)}}}
    res = http_request("PATCH", gist_url, headers=headers, json=body)

    if res.status_code in (401, 403):
        raise FatalError(
            f"Gist-API:et nekade skrivning ({res.status_code}). Token saknar "
            f"skrivrättighet (scopet 'gist') eller är utgången."
        )
    if res.status_code != 200:
        raise FatalError(f"Kunde inte uppdatera gisten ({res.status_code}): {res.text[:200]}")


# --- Main -------------------------------------------------------------------

def run() -> int:
    gist_id = os.environ.get("GIST_ID")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GIST_TOKEN")
    missing = [name for name, value in (("GIST_ID", gist_id), ("GITHUB_TOKEN", token)) if not value]
    if missing:
        raise FatalError(f"Saknar miljövariabel: {', '.join(missing)}")

    now_local = datetime.now(TZ)
    target_day, hour_prices = pick_target(now_local)
    hours, best_sum = find_cheapest_consecutive_block(hour_prices, BLOCK_SIZE)

    payload = {
        "hours": hours,
        "block_size": BLOCK_SIZE,
        "best_sum": round(best_sum, 6),
        "updated": now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": target_day.strftime("%Y-%m-%d"),
        "area": AREA,
    }

    gist_url = f"https://api.github.com/gists/{gist_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    gist = load_gist(gist_url, headers)
    existing = read_existing_plan(gist, GIST_FILENAME)

    # Skriv aldrig över en plan för i morgon med en plan för i dag.
    if existing is not None and target_day == now_local.date():
        existing_date = str(existing.get("date", ""))
        if existing_date > payload["date"]:
            print(f"⏭️  Gisten har redan planen för {existing_date}. "
                  f"Skriver inte över den med {payload['date']}.")
            return 0

    patch_gist(gist_url, headers, GIST_FILENAME, payload)
    print(f"✅ Gist uppdaterad ({GIST_FILENAME}): "
          f"{json.dumps(payload, ensure_ascii=False)}")
    return 0


def main() -> int:
    try:
        return run()
    except FatalError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1
    except PricesNotPublished as exc:
        print(f"ℹ️  Inget att göra: {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
