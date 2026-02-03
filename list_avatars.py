
import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = "sk_V2_hgu_kjihuKdHmVc_AQcImD5QXm1s4o4V8KunsKmlo0LjOuDV"
API_URL = "https://api.heygen.com/v2/avatars"

headers = {
    "X-Api-Key": API_KEY,
    "Content-Type": "application/json"
}

try:
    response = requests.get(API_URL, headers=headers)
    response.raise_for_status()
    data = response.json()
    
    avatars = data.get("data", {}).get("avatars", [])
    print(f"Found {len(avatars)} avatars.")
    
    # Save to file for inspection
    with open('avatars_dump.json', 'w', encoding='utf-8') as f:
        json.dump(avatars, f, indent=2, ensure_ascii=False)
    print("Saved all avatars to avatars_dump.json")
    
    target_name = "Huykkk"
    found = False
    
    print("Checking My Avatars (Private)...")
    url = "https://api.heygen.com/v2/avatars"
    # To get private avatars specifically, usually standard endpoint works but filter is needed
    # Or checking V1 list which sometimes exposes them differently
    
    # Let's try to look for the ID directly in the dump if present
    target_group_id = "4a4cbf45415048f79c6c7968e353a359"
    
    # Try getting avatar details directly by ID if there is such endpoint
    # (HeyGen doesn't document 'get single avatar' well, but let's try)
    
    print(f"Scanning 1287 avatars for anything related to group {target_group_id}...")
    found_any = False
    for avatar in avatars:
        # Check all fields for the ID
        fields_str = str(avatar)
        if target_group_id in fields_str:
             print(f"FOUND MATCH in Avatar Data! \nData: {avatar}")
             found_any = True
             
    if not found_any:
        print("Still nothing found searching by Group ID in the full list.")
        
    # Try V1 endpoint just in case
    print("\nChecking V1 Avatars List...")
    url_v1 = "https://api.heygen.com/v1/avatar.list"
    try:
        resp_v1 = requests.get(url_v1, headers=headers)
        if resp_v1.status_code == 200:
             data_v1 = resp_v1.json().get("data", {}).get("avatars", [])
             print(f"Found {len(data_v1)} avatars in V1 API.")
             for av in data_v1:
                 if "Huykkk" in str(av) or target_group_id in str(av):
                      print(f"FOUND IN V1: {av}")
        else:
            print(f"V1 API check failed: {resp_v1.status_code}")
    except Exception as e:
        print(f"V1 Check Error: {e}")

except Exception as e:
    print(f"Error: {str(e)}")

