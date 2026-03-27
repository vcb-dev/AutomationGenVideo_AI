import requests
import json

TOKEN = "EAAdPQBbx5BQBRMhTEkGyO3ZB5ZBluEmOkZCReTZCmxzuld7BjNrTa4KzHsXLSEkeN8F81eNOcqTQZBx5XZAwtdZAZAgudMlYZBklcqRecr2vXRZBgSqQrNZCR2UXHq2WZAzVTCx8UeVjsQteS04CJ70sfJA9qcRVDVvMe6LH8KQfss6314sS1Lgns7b6GjTOUi9ORyZAOiAPS4JSUBvF0bIaXBhZARPVNdk26y7iTQ8BP48k0ZD"

# Inspect token
r = requests.get("https://graph.facebook.com/debug_token", params={"input_token": TOKEN, "access_token": TOKEN})
data = r.json()
print("Token Info:")
print(json.dumps(data, indent=2))
