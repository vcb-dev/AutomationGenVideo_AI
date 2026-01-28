"""
Apify Setup & Verification Script

Usage:
1. Get your API Token from: https://console.apify.com/account/integrations
2. Add to .env file: APIFY_API_TOKEN=your_token_here
3. Run this script: python setup_apify.py
"""

import os
import sys
from dotenv import load_dotenv

# Load env variables
load_dotenv()

def check_apify():
    token = os.getenv('APIFY_API_TOKEN')
    
    print("="*50)
    print("🪄  Apify Setup Check")
    print("="*50)
    
    if not token:
        print("❌ Error: APIFY_API_TOKEN not found in environment variables.")
        print("   Please add it to your .env file.")
        print("   Example: APIFY_API_TOKEN=apify_api_123456789")
        return False
        
    print(f"✅ Token found: {token[:10]}..." + "*"*5)
    
    try:
        print("\n🔄 Installing apify-client if needed...")
        os.system("pip install apify-client")
        
        print("\n🔄 Testing connection to Apify...")
        from apify_client import ApifyClient
        
        client = ApifyClient(token)
        user = client.user().get()
        
        if user:
            print(f"✅ Connection Successful!")
            print(f"   User ID: {user.get('id')}")
            print(f"   Username: {user.get('username')}")
            # Check limits if available in user object, otherwise assume ok
            print("\n✅ Apify is ready to use!")
            return True
        else:
            print("❌ Failed to get user info. Invalid token?")
            return False
            
    except ImportError:
        print("❌ apify-client not installed. Please run: pip install apify-client")
        return False
    except Exception as e:
        print(f"❌ Connection failed: {str(e)}")
        return False

if __name__ == "__main__":
    check_apify()
