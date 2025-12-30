import os
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

AREA = "SE3"
BLOCK_SIZE = 4
TZ = ZoneInfo("Europe/Stockholm")

# Sätt till exakt filnamn i din gist (rekommenderas).
# Om du vill behålla "första filen i gisten", sätt GIST_FILENAME = "".
GIST_FILENAME = "elpris_3h.json"


def fetch_hourly_prices_for_date(year: int, mm_dd: str) -> dict[int, float]:
    """
    Hämtar elprisdata från elprisetjustnu och returnerar timpriser 0-23.
    Stödjer både:
      - 24 datapunkter (1 per timme)
      - 96 datapunkter (4 per timme/kvart) -> timmedel
    """
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{mm_dd}_{AREA}.json"
    res = requests.get(url, timeout=15)
    res.raise_for_status()
    data = res.json()

    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    for entry in data:
        h = int(entry["time_start"][11:13])
        buckets[h].append(float(entry["SEK_per_kWh"]))

    counts = sorted(set(len(buckets[h]) for h in range(24)))

    # Timprisformat (1 post per timme)
    if counts == [1]:
        return {h: buckets[h][0] for h in range(24)}

    # Kvartsprisformat (4 poster per timme) -> timmedel
    if counts == [4]:
        return {h: sum(buckets[h]) / 4.0 for h in range(24)}

    # Annars: ofullständig eller oväntad granularitet
    raise RuntimeError(f"Unexpected data granularity per hour. Counts={counts}")


def find_cheapest_consecutive_block(hour_prices: dict[int, float], block_size: int) -> tuple[list[int], float]:
    """
    Returnerar billigaste sammanhängande blocket (t.ex. 3 timmar) och dess summa.
    Tie-breaker: tidigaste blocket vid lika summa.
    """
    if set(hour_prices.keys()) != set(range(24)):
        missing = sorted(set(range(24)) - set(hour_prices.keys()))
        raise RuntimeError(f"Missing hours: {missing}")

    best_start = 0
    best_sum = float("inf")

    for start in range(0, 24 - block_size + 1):
        total = sum(hour_prices[start + i] for i in range(block_size))
        if total < best_sum:
            best_sum = total
            best_start = start

    return list(range(best_start, best_start + block_size)), best_sum


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
    year = now_local.year
    mm_dd = now_local.strftime("%m-%d")

    hour_prices = fetch_hourly_prices_for_date(year, mm_dd)
    hours, best_sum = find_cheapest_consecutive_block(hour_prices, BLOCK_SIZE)

    payload = {
        "hours": hours,
        "block_size": BLOCK_SIZE,
        "best_sum": round(best_sum, 6),
        "updated": now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": now_local.strftime("%Y-%m-%d"),
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
