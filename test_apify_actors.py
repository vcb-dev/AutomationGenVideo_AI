import requests
resp = requests.get('https://api.apify.com/v2/actor-store/actors?search=facebook')
d = resp.json()
if 'data' in d and 'items' in d['data']:
    for i in d['data']['items'][:20]:
        print(f"{i['name']} ({i.get('username')}) - {i.get('title')} - {i.get('pricingInfo')}")
else:
    print(d)
