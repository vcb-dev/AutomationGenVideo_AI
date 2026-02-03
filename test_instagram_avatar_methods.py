"""
Test different methods to get Instagram avatar URL
"""
import requests
from bs4 import BeautifulSoup
import json
import re

def method1_public_profile_pic(username):
    """Method 1: Instagram public profile pic endpoint"""
    url = f"https://www.instagram.com/{username}/profile_pic.jpg"
    print(f"\n{'='*80}")
    print(f"Method 1: Public Profile Pic Endpoint")
    print(f"URL: {url}")
    
    try:
        response = requests.get(url, allow_redirects=True, timeout=10)
        print(f"Status: {response.status_code}")
        print(f"Final URL: {response.url}")
        print(f"Content-Type: {response.headers.get('content-type')}")
        print(f"Content-Length: {len(response.content)} bytes")
        return response.url if response.status_code == 200 else None
    except Exception as e:
        print(f"Error: {e}")
        return None

def method2_scrape_profile_page(username):
    """Method 2: Scrape from public profile page"""
    url = f"https://www.instagram.com/{username}/"
    print(f"\n{'='*80}")
    print(f"Method 2: Scrape Profile Page")
    print(f"URL: {url}")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        print(f"Status: {response.status_code}")
        
        # Try to find JSON data in script tags
        soup = BeautifulSoup(response.text, 'html.parser')
        scripts = soup.find_all('script', type='application/ld+json')
        
        for script in scripts:
            try:
                data = json.loads(script.string)
                if 'image' in data:
                    print(f"Found avatar in LD+JSON: {data['image'][:100]}...")
                    return data['image']
            except:
                pass
        
        # Try to find in meta tags
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            print(f"Found avatar in og:image: {og_image['content'][:100]}...")
            return og_image['content']
        
        print("No avatar found in page")
        return None
        
    except Exception as e:
        print(f"Error: {e}")
        return None

def method3_instagram_json_api(username):
    """Method 3: Instagram JSON API (may be blocked)"""
    url = f"https://www.instagram.com/{username}/?__a=1&__d=dis"
    print(f"\n{'='*80}")
    print(f"Method 3: Instagram JSON API")
    print(f"URL: {url}")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        response = requests.get(url, headers=headers, timeout=10)
        print(f"Status: {response.status_code}")
        
        if response.status_code == 200:
            try:
                data = response.json()
                # Try different paths where avatar might be
                paths = [
                    'graphql.user.profile_pic_url_hd',
                    'graphql.user.profile_pic_url',
                    'user.profile_pic_url_hd',
                    'user.profile_pic_url',
                ]
                
                for path in paths:
                    keys = path.split('.')
                    value = data
                    for key in keys:
                        value = value.get(key, {})
                        if not value:
                            break
                    if value and isinstance(value, str):
                        print(f"Found avatar at {path}: {value[:100]}...")
                        return value
                
                print("No avatar found in JSON response")
            except:
                print("Response is not valid JSON")
        
        return None
        
    except Exception as e:
        print(f"Error: {e}")
        return None

if __name__ == "__main__":
    username = "huyk_mekimhoan"
    
    print(f"\n{'#'*80}")
    print(f"# Testing Instagram Avatar Extraction Methods for: @{username}")
    print(f"{'#'*80}")
    
    # Test all methods
    avatar1 = method1_public_profile_pic(username)
    avatar2 = method2_scrape_profile_page(username)
    avatar3 = method3_instagram_json_api(username)
    
    # Summary
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"Method 1 (Public Endpoint): {avatar1 if avatar1 else 'FAILED'}")
    print(f"Method 2 (Scrape Page):      {avatar2[:80] if avatar2 else 'FAILED'}...")
    print(f"Method 3 (JSON API):         {avatar3[:80] if avatar3 else 'FAILED'}...")
    
    # Recommend best method
    print(f"\n{'='*80}")
    if avatar1:
        print("✅ RECOMMENDED: Use Method 1 (Public Endpoint)")
        print(f"   URL: https://www.instagram.com/{username}/profile_pic.jpg")
    elif avatar2:
        print("✅ RECOMMENDED: Use Method 2 (Scrape Page)")
    elif avatar3:
        print("✅ RECOMMENDED: Use Method 3 (JSON API)")
    else:
        print("❌ All methods failed - stick with Apify")
