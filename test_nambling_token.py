import requests

TOKEN = "EAAdPQBbx5BQBRMhTEkGyO3ZB5ZBluEmOkZCReTZCmxzuld7BjNrTa4KzHsXLSEkeN8F81eNOcqTQZBx5XZAwtdZAZAgudMlYZBklcqRecr2vXRZBgSqQrNZCR2UXHq2WZAzVTCx8UeVjsQteS04CJ70sfJA9qcRVDVvMe6LH8KQfss6314sS1Lgns7b6GjTOUi9ORyZAOiAPS4JSUBvF0bIaXBhZARPVNdk26y7iTQ8BP48k0ZD"

print("=== Test /me với Page Token ===\n")

# Lấy info của chính page token
r = requests.get("https://graph.facebook.com/v20.0/me",
    params={"fields": "id,name,fan_count,followers_count", "access_token": TOKEN})
data = r.json()
print("GET /me:", data)

print("\n=== Test /me/posts ===")
r2 = requests.get("https://graph.facebook.com/v20.0/me/posts",
    params={"fields": "id,message,created_time,permalink_url", "limit": 5, "access_token": TOKEN})
data2 = r2.json()
print("GET /me/posts:", data2)

print("\n=== Test /me/feed ===")
r3 = requests.get("https://graph.facebook.com/v20.0/me/feed",
    params={"fields": "id,message,created_time,permalink_url", "limit": 5, "access_token": TOKEN})
data3 = r3.json()
print("GET /me/feed:", data3)
