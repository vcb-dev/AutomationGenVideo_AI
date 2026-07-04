import os
import sys
from anthropic import Anthropic

def test_claude():
    key = "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"
    client = Anthropic(api_key=key)
    
    models = [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5"
    ]
    
    for model in models:
        print(f"Testing model: {model}")
        try:
            message = client.messages.create(
                model=model,
                max_tokens=10,
                messages=[{"role": "user", "content": "Hi"}]
            )
            print(f"  SUCCESS: {message.content[0].text}")
            return
        except Exception as e:
            print(f"  FAILED: {e}")

if __name__ == "__main__":
    test_claude()
