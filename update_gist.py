import requests, json, os
from datetime import datetime, timedelta

def get_swedish_date():
    now = datetime.utcnow()
    year = now.year
    march = datetime(year, 3, 31)
    while march.weekday() != 6:
        march -= timedelta(days=1)
    october = datetime(year, 10, 31)
    while october.weekday() != 6:
        october -= timedelta(days=1)
    is_dst = march <= now < october
    offset = timedelta(hours=2 if is_dst else 1)
    return now + offset

# Hämta svensk lokal tid och sätt upp API-URL
local = get_swedish_date()
year = local.year
date_str = local.strftime('%m-%d')
url = f'https://www.elprisetjustnu.se/api/v1/prices/{year}/{date_str}_SE3.json'

# Hämta elprisdata
res = requests.get(url, timeout=5)
res.raise_for_status()
data = res.json()

# Skapa dict: { timme: pris }
hour_prices = {}
for entry in data:
    hour = int(entry["time_start"][11:13])
    hour_prices[hour] = entry["SEK_per_kWh"]

# Hitta billigaste 4 sammanhängande timmar
best_start = None
best_sum = float("inf")

for start in range(0, 24 - 4 + 1):  # 0–20
    try:
        prices = [hour_prices[start + i] for i in range(4)]
        total = sum(prices)
        if total < best_sum:
            best_sum = total
            best_start = start
    except KeyError:
        continue  # hoppa om data saknas

# Skapa payload
if best_start is not None:
    cheapest_block = [best_start + i for i in range(4)]
else:
    cheapest_block = [0, 1, 2, 3]  # fallback (bör inte inträffa)

payload = {
    'hours': cheapest_block,
    'updated': datetime.now().strftime('%Y-%m-%d %H:%M')
}

# Uppdatera Gist
gist_url = f'https://api.github.com/gists/{os.environ["GIST_ID"]}'
headers = {'Authorization': f'token {os.environ["GITHUB_TOKEN"]}'}
r = requests.get(gist_url, headers=headers)
r.raise_for_status()
gist = r.json()
filename = list(gist['files'].keys())[0]
gist['files'][filename]['content'] = json.dumps(payload)
res = requests.patch(gist_url, headers=headers, json={ 'files': gist['files'] })
res.raise_for_status()
print('✅ Gist updated:', payload)
