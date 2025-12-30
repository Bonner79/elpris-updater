import requests, json, os
from datetime import datetime
from zoneinfo import ZoneInfo

AREA = "SE3"
BLOCK_SIZE = 2
TZ = ZoneInfo("Europe/Stockholm")

def fetch_hourly_prices_for_date(year: int, mm_dd: str) -> dict[int, float]:
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{mm_dd}_{AREA}.json"
    res = requests.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()

    buckets = {h: [] for h in range(24)}
    for entry in data:
        h = int(entry["time_start"][11:13])
        buckets[h].append(float(entry["SEK_per_kWh"]))

    # Kräver exakt 4 kvart per timme
    for h in range(24):
        if len(buckets[h]) != 4:
            raise RuntimeError(f"Incomplete quarter data for hour {h}: {len(buckets[h])}/4")

    # Medelpris per timme (4 lika långa intervall)
    return {h: sum(buckets[h]) / 4.0 for h in range(24)}

def find_cheapest_consecutive_block(hour_prices: dict[int, float], block_size: int):
    if set(hour_prices.keys()) != set(range(24)):
        missing = sorted(set(range(24)) - set(hour_prices.keys()))
        raise RuntimeError(f"Missing hours: {missing}")

    best_start = 0
    best_sum = float("inf")

    for start in range(0, 24 - block_size + 1):
        total = sum(hour_prices[start + i] for i in range(block_size))
        if total < best_sum:  # tie-breaker: tidigast vid lika
            best_sum = total
            best_start = start

    return list(range(best_start, best_start + block_size)), best_sum

now_local = datetime.now(TZ)
year = now_local.year
mm_dd = now_local.strftime("%m-%d")

hour_prices = fetch_hourly_prices_for_date(year, mm_dd)
hours, best_sum = find_cheapest_consecutive_block(hour_prices, BLOCK_SIZE)

payload = {
    "hours": hours,
    "block_size": BLOCK_SIZE,
    "best_sum": best_sum,
    "updated": now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
    "date": now_local.strftime("%Y-%m-%d"),
    "area": AREA
}

gist_url = f"https://api.github.com/gists/{os.environ['GIST_ID']}"
headers = {"Authorization": f"token {os.environ['GITHUB_TOKEN']}"}

r = requests.get(gist_url, headers=headers, timeout=10)
r.raise_for_status()
gist = r.json()

filename = list(gist["files"].keys())[0]
patch_body = {"files": {filename: {"content": json.dumps(payload, ensure_ascii=False)}}}
res = requests.patch(gist_url, headers=headers, json=patch_body, timeout=10)
res.raise_for_status()

print("✅ Gist updated:", payload)
