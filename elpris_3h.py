import os
import json
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

AREA = "SE3"
BLOCK_SIZE = 3
TZ = ZoneInfo("Europe/Stockholm")

# Exakt filnamn i gisten som ska uppdateras (en fil per blockstorlek).
GIST_FILENAME = "elpris_3h.json"


def fetch_hourly_prices_for_date(year: int, mm_dd: str) -> dict[int, float]:
    """
    Hämtar elprisdata från elprisetjustnu och returnerar timpriser.
    Stödjer både:
      - 24 datapunkter (1 per timme)
      - 96 datapunkter (4 per timme/kvart) -> timmedel
    Klarar även DST-dygn med 23 eller 25 timmar.
    """
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{mm_dd}_{AREA}.json"
    res = requests.get(url, timeout=15)
    res.raise_for_status()
    data = res.json()

    buckets: dict[int, list[float]] = {}
    for entry in data:
        h = int(entry["time_start"][11:13])
        buckets.setdefault(h, []).append(float(entry["SEK_per_kWh"]))

    if len(buckets) < 23:
        raise RuntimeError(f"För få timmar i prisdatan: {sorted(buckets)}")

    return {h: sum(v) / len(v) for h, v in buckets.items()}


def find_cheapest_consecutive_block(hour_prices: dict[int, float], block_size: int) -> tuple[list[int], float]:
    """
    Returnerar billigaste sammanhängande blocket och dess summa.
    Tie-breaker: tidigaste blocket vid lika summa.
    """
    hours = sorted(hour_prices)
    if len(hours) < block_size:
        raise RuntimeError(f"För få timmar för block_size={block_size}: {hours}")

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
        raise RuntimeError("Hittade inget sammanhängande block")

    return hours[best_idx:best_idx + block_size], best_sum


def resolve_gist_filename(gist_json: dict) -> str:
    """
    Väljer filen i gisten som ska uppdateras.
    - Om GIST_FILENAME är satt: använd den.
    - Annars: första filen i gisten.
    """
    if GIST_FILENAME:
        return GIST_FILENAME
    return list(gist_json["files"].keys())[0]


def main():
    now_local = datetime.now(TZ)

    # Day-ahead: vi räknar på MORGONDAGENS priser så att planen ligger
    # i gisten före midnatt, när Shelly hämtar den 00:15.
    target = now_local + timedelta(days=1)

    hour_prices = fetch_hourly_prices_for_date(target.year, target.strftime("%m-%d"))
    hours, best_sum = find_cheapest_consecutive_block(hour_prices, BLOCK_SIZE)

    payload = {
        "hours": hours,
        "block_size": BLOCK_SIZE,
        "best_sum": round(best_sum, 6),
        "updated": now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": target.strftime("%Y-%m-%d"),
        "area": AREA
    }

    gist_url = f"https://api.github.com/gists/{os.environ['GIST_ID']}"
    headers = {"Authorization": f"token {os.environ['GITHUB_TOKEN']}"}

    r = requests.get(gist_url, headers=headers, timeout=15)
    r.raise_for_status()
    gist = r.json()

    filename = resolve_gist_filename(gist)

    patch_body = {
        "files": {
            filename: {
                "content": json.dumps(payload, ensure_ascii=False)
            }
        }
    }

    res = requests.patch(gist_url, headers=headers, json=patch_body, timeout=15)
    res.raise_for_status()

    print("✅ Gist updated:", payload)


if __name__ == "__main__":
    main()
