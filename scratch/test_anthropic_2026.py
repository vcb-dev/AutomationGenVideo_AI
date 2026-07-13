import os
import sys
from anthropic import Anthropic

def test_claude():
    key = "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"
    client = Anthropic(api_key=key)
    
    # Try common 2025/2026 patterns based on search
    models = [
        "claude-4-7-opus-20260416",
        "claude-4-6-sonnet-20260215",
        "claude-4-5-haiku-20251022",
        "claude-4-opus",
        "claude-4-sonnet",
        "claude-4-haiku",
        "claude-3-5-sonnet-latest",
        "claude-3-5-haiku-latest"
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
