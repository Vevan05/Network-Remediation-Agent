"""
Connectivity check for the LLM provider. Not a test - it needs a live API key
and a network call, so it lives outside tests/ and is run by hand:

    python scripts/check_llm.py
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY is missing (check .env)", file=sys.stderr)
        return 1

    from google import genai

    client = genai.Client(api_key=api_key)
    try:
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
            contents="Reply with exactly: Gemini authentication works",
        )
        print(response.text)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
