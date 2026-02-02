
import requests
import json

API_KEY = "sk_V2_hgu_k7qqfeBEnoJ_QW8I0qU6WPBw7V2A126JVClHWoExtBkx"
VIDEO_ID = "0a309f9d487841498899f69efc741be2"
URL = f"https://api.heygen.com/v1/video_status.get?video_id={VIDEO_ID}"

headers = {
    "X-Api-Key": API_KEY,
    "Accept": "application/json"
}

try:
    print(f"Checking status for video {VIDEO_ID}...")
    response = requests.get(URL, headers=headers)
    print(f"Status Code: {response.status_code}")
    print("Response Body:")
    print(json.dumps(response.json(), indent=2))
except Exception as e:
    print(f"Error: {e}")
