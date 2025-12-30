import requests, json, os
from datetime import datetime
from zoneinfo import ZoneInfo

BLOCK_SIZE = 1
AREA = "SE3"
TZ = ZoneInfo("Europe/Stockholm")

now_local = datetime.now(TZ)
year = now_local.year
date_str = now_local.strftime("%m-%d")

url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{date_str}_{AREA}.json"

res = requests.get(url, timeout=10)
res.raise_for_status()
data = res.json()

hour_prices = {}
for entry in data:
    hour = int(entry["time_start"][11:13])
    hour_prices[hour] = float(entry["SEK_per_kWh"])

# Fallback om API inte gav full dygnslista
if len(hour_prices) < 24:
    cheapest_hours = [0]
else:
    # Billigaste timmen (tie-breaker: tidigast om lika)
    best_hour = min(hour_prices.items(), key=lambda kv: (kv[1], kv[0]))[0]
    cheapest_hours = [best_hour]

payload = {
    "hours": cheapest_hours,
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
