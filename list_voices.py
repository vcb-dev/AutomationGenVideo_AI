
import requests
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = "sk_V2_hgu_k4tdbzfKRK3_N3yyTe9xk9HvlWwuDe4eRs3auOxaEvov"
API_URL = "https://api.heygen.com/v2/voices"

headers = {
    "X-Api-Key": API_KEY,
    "Content-Type": "application/json"
}

try:
    response = requests.get(API_URL, headers=headers)
    response.raise_for_status()
    data = response.json()
    
    voices = data.get("data", {}).get("voices", [])
    print(f"Found {len(voices)} voices.")
    
    # Filter for Vietnamese voices
    vi_voices = [v for v in voices if "Vietnamese" in v.get("language", "") or "vi" in v.get("language", "")]
    
    if vi_voices:
        print("\n--- Vietnamese Voices ---")
        for voice in vi_voices:
            print(f"ID: {voice.get('voice_id')} - Name: {voice.get('name')} - Gender: {voice.get('gender')}")
    else:
        print("\nNo Vietnamese voices found in the default list.")
        
    print("\n--- First 10 Voices (Any Language) ---")
    for voice in voices[:10]:
         print(f"ID: {voice.get('voice_id')} - Name: {voice.get('name')} - Lang: {voice.get('language')}")
        
except Exception as e:
    print(f"Error: {str(e)}")
    if 'response' in locals():
        print(response.text)
