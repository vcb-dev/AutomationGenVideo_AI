
import json
import os

try:
    with open('e:\\Workspace\\VietChiBaoPrj\\AutomationGenVideo_AI\\facebook_apify_test_results.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
        
        print(f"Checking {data.get('actor_id')} results...")
        
        for t in data.get('tests', []):
            print(f"\n--- Checking Test: {t.get('config')} ---")
            item = t.get('sample_item', {})
            
            # Recursive search for relevant keys
            def search_keys(node, path=""):
                if isinstance(node, dict):
                    for k, v in node.items():
                        # Check for follower/like related keys
                        key_lower = k.lower()
                        if 'follower' in key_lower or ('like' in key_lower and 'count' in key_lower) or 'fans' in key_lower:
                            print(f"Found KEY '{k}' at '{path}': {v} (Type: {type(v).__name__})")
                        
                        search_keys(v, f"{path}.{k}" if path else k)
                elif isinstance(node, list):
                    # check first few items in list
                    for i, v in enumerate(node[:1]): 
                        search_keys(v, f"{path}[{i}]")

            search_keys(item)
            
            print(f"User Object: {json.dumps(item.get('user', {}), indent=2)}")

except Exception as e:
    print(f"Error: {e}")
