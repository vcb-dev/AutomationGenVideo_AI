import os
import requests
import json
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_deepseek():
    api_key = "sk-d1bf6f8d2d6846de8f1fdf6fd1261302"
    url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "You are a translator."},
            {"role": "user", "content": "Translate 'Hello' to Vietnamese."}
        ],
        "temperature": 0.1,
        "max_tokens": 10
    }

    try:
        logger.info("Testing DeepSeek connection...")
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        result = response.json()
        content = result['choices'][0]['message']['content']
        logger.info(f"Success! Response: {content}")
        return True
    except Exception as e:
        logger.error(f"DeepSeek test failed: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Response body: {e.response.text}")
        return False

if __name__ == "__main__":
    test_deepseek()
