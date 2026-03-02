import requests
import json

url = 'https://api.apify.com/v2/actor-store/actors?search=facebook%20search&limit=10'
res = requests.get(url)
data = res.json()

print(data)
