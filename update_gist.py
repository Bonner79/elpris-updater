import requests, json, os
from datetime import datetime
from zoneinfo import ZoneInfo

BLOCK_SIZE = 4
AREA = "SE3"
TZ = ZoneInfo("Europe/Stockholm")

# Lokal tid i Sverige (DST hanteras automatiskt)
now_local = datetime.now(TZ)
year = now_local.year
date_str = now_local.strftime("%m-%d")

url = f"https://www.elprisetjustnu.se/api/v1/prices/{year}/{date_str}_{AREA}.json"

# Hämta elprisdata
res = requests.get(url, timeout=10)
res.raise_for_status()
data = res.json()

# Skapa dict: { timme: pris }
hour_prices = {}
for entry in data:
    hour = int(entry["time_start"][11:13])
    hour_prices[hour] = float(entry["SEK_per_kWh"])

# Hitta billigaste 4 sammanhängande timmar
if len(hour_prices) < 24:
    cheapest_block = [0, 1, 2, 3]  # fallback om dygnsdata saknas
else:
    best_start = 0
    best_sum = float("inf")

    for start in range(0, 24 - BLOCK_SIZE + 1):  # 0–20
        total = sum(hour_prices[start + i] for i in range(BLOCK_SIZE))
        # tie-breaker: vid lika pris -> välj tidigaste blocket
        if total < best_sum:
            best_sum = total
            best_start = start

    cheapest_block = list(range(best_start, best_start + BLOCK_SIZE))

payload = {
    "hours": cheapest_block,
    "updated": now_local.strftime("%Y-%m-%d %H:%M:%S %Z"),
    "date": now_local.strftime("%Y-%m-%d"),
    "area": AREA
}

# Uppdatera Gist
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
