
import requests
import os
from dotenv import load_dotenv
import json

load_dotenv()

API_KEY = os.getenv("HEYGEN_API_KEY")
TARGET_GROUP_ID = "871cc4b87b8643c6b9cc6b4cf6797fc9"
HEADERS = {
    "X-Api-Key": API_KEY,
    "Content-Type": "application/json"
}

    print(f"\n--- Checking Specific Avatar ID: {TARGET_GROUP_ID} ---")
    url = f"https://api.heygen.com/v2/avatar/{TARGET_GROUP_ID}" # Try hypothetical endpoint
    # Note: v2 endpoint for single avatar isn't clearly documented but usually follows REST
    
    # Also valid V2 way might be ?avatar_id=...
    
    # But let's verify via 'create video' check (dry run) logic if possible, 
    # OR assume it exists and debug the output.
    
    # Actually, let's look at the Private Avatars from V1 one last time carefully
    # If not found, we will TRUST THE USER and proceed to update the environment to use this ID.
    pass

# 1. Standard V2
test_endpoint("V2 Standard", "https://api.heygen.com/v2/avatars")

# 2. V2 with type=my
test_endpoint("V2 Type=my", "https://api.heygen.com/v2/avatars", {"type": "my"})

# 3. V2 with type=private
test_endpoint("V2 Type=private", "https://api.heygen.com/v2/avatars", {"type": "private"})

# 4. Talking Photos V2 (Just in case)
test_endpoint("Thinking Photo V2", "https://api.heygen.com/v2/talking_photos")

# 5. V1 List
test_endpoint("V1 List", "https://api.heygen.com/v1/avatar.list")

