
import os
import django
from django.conf import settings
from apify_client import ApifyClient

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

def test_tiktok_actor(username):
    api_token = getattr(settings, "APIFY_API_TOKEN", "")
    if not api_token:
        print("API Token not found")
        return

    client = ApifyClient(api_token)
    actor_id = "clockworks/free-tiktok-scraper"
    
    clean_user = username.replace("@", "").strip()
    # Check if it's a URL and extract username
    if "tiktok.com/" in clean_user:
        if "@" in clean_user:
            clean_user = clean_user.split("@")[-1].split("/")[0].split("?")[0]
        else:
            # Handle case like tiktok.com/username
            clean_user = clean_user.split("tiktok.com/")[-1].split("/")[0].split("?")[0]

    print(f"Testing with clean_user: {clean_user}")
    
    run_input = {"profiles": [clean_user], "resultsPerPage": 5}
    
    try:
        print(f"Calling actor {actor_id}...")
        run = client.actor(actor_id).call(run_input=run_input, timeout_secs=120)
        print(f"Status: {run['status']}")
        if run['status'] != 'SUCCEEDED':
            print(f"Full run object: {run}")
        else:
            print("Success!")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_tiktok_actor("https://www.tiktok.com/@huyk.trangsucvienchi")
