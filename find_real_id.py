
import json
import os

TARGET_ID = "871cc4b87b8643c6b9cc6b4cf6797fc9"

def search_dump():
    if not os.path.exists('avatars_dump.json'):
        print("avatars_dump.json not found.")
        return

    print(f"Searching for {TARGET_ID} in avatars_dump.json...")
    
    with open('avatars_dump.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    found = False
    for avatar in data:
        # Check if target ID appears in any value of the avatar dict
        # Common fields: avatar_id, group_id, etc.
        blob = json.dumps(avatar)
        if TARGET_ID in blob:
            print("\n!!! FOUND MATCH !!!")
            print(f"Avatar Name: {avatar.get('name') or avatar.get('avatar_name')}")
            print(f"Avatar ID (Use this one): {avatar.get('avatar_id')}")
            print(f"Group ID: {avatar.get('group_id')}")
            print("-" * 20)
            print(json.dumps(avatar, indent=2))
            found = True
            
    if not found:
        print("Not found in dump. This suggests the API list endpoint isn't returning this Private avatar.")

if __name__ == "__main__":
    search_dump()
