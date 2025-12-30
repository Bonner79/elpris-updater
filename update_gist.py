import requests, json, os
from datetime import datetime
from zoneinfo import ZoneInfo

AREA = "SE3"
BLOCK_SIZE = 4  # ändra till 1/2/4 osv
TZ = ZoneInfo("Europe/Stockholm")

def fetch_prices_for_date(year: int, mm_dd: str):
    url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{mm_dd}_{AREA}.json"
    res = requests.get(url, timeout=10)
    res.raise_for_status()
    data = res.json()

    hour_prices = {}
    for entry in data:
        h = int(entry["time_start"][11:13])
        hour_prices[h] = float(entry["SEK_per_kWh"])
    return hour_prices

def find_cheapest_consecutive_block(hour_prices: dict[int, float], block_size: int) -> list[int]:
    # Hårt krav: måste ha 24 timmar, annars ska vi inte riskera fel styrning
    if set(hour_prices.keys()) != set(range(24)):
        missing = sorted(set(range(24)) - set(hour_prices.keys()))
        raise RuntimeError(f"Price data incomplete. Missing hours: {missing}")

    best_start = 0
    best_sum = float("inf")

    for start in range(0, 24 - block_size + 1):
        total = sum(hour_prices[start + i] for i in range(block_size))
        # tie-breaker: tidigaste blocket om lika
        if total < best_sum:
            best_sum = total
            best_start = start

    return list(range(best_start, best_start + block_size)), best_sum

now_local = datetime.now(TZ)
year = now_local.year
mm_dd = now_local.strftime("%m-%d")

hour_prices = fetch_prices_for_date(year, mm_dd)
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
content = json.dumps(payload, ensure_ascii=False)

patch_body = {"files": {filename: {"content": content}}}
res = requests.patch(gist_url, headers=headers, json=patch_body, timeout=10)
res.raise_for_status()

print("✅ Gist updated:", payload)
