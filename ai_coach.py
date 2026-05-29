import os
import sys
import argparse
from dotenv import load_dotenv
from prompt import PROMPT

# Try to import SDKs (only fail at runtime if the chosen provider's lib is missing)
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None

SYSTEM_INSTRUCTION = PROMPT

def main():
    # Load environment variables from a .env file if it exists
    load_dotenv()
    
    parser = argparse.ArgumentParser(description="Executive Leadership AI Coach")
    parser.add_argument(
        "--provider",
        type=str,
        default=os.environ.get("AI_PROVIDER", "openai").lower(),
        choices=["openai", "claude"],
        help="AI provider to use: 'openai' (default) or 'claude'",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model name to use (defaults: gpt-4o for OpenAI, claude-sonnet-4-5 for Claude)",
    )
    args = parser.parse_args()

    DEFAULT_MODELS = {"openai": "gpt-4o", "claude": "claude-sonnet-4-5"}
    model = args.model or os.environ.get("AI_MODEL") or DEFAULT_MODELS[args.provider]

    if args.provider == "claude":
        api_key = os.environ.get("CLAUDE_API_KEY")
        if not api_key:
            print("Error: CLAUDE_API_KEY environment variable is not set.")
            print("Please set it in your terminal, or create a '.env' file with CLAUDE_API_KEY=your_key")
            sys.exit(1)
        if Anthropic is None:
            print("Error: The 'anthropic' library is not installed. Run: pip install -r requirements.txt")
            sys.exit(1)
        print(f"Initializing Executive AI Coach (Provider: Claude | Model: {model})...")
        client = Anthropic(api_key=api_key)
    else:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("Error: OPENAI_API_KEY environment variable is not set.")
            print("Please set it in your terminal, or create a '.env' file with OPENAI_API_KEY=your_key")
            sys.exit(1)
        if OpenAI is None:
            print("Error: The 'openai' library is not installed. Run: pip install -r requirements.txt")
            sys.exit(1)
        print(f"Initializing Executive AI Coach (Provider: OpenAI | Model: {model})...")
        client = OpenAI(api_key=api_key)

    messages = [
        {"role": "system", "content": SYSTEM_INSTRUCTION}
    ]

    print("\n" + "="*60)
    print("Welcome to your Executive Leadership Coaching Session.")
    print("Type 'exit' or 'quit' to end the session.")
    print("="*60 + "\n")

    while True:
        try:
            user_input = input("\nYou: ")
            if user_input.strip().lower() in ['exit', 'quit']:
                print("\nCoach: Thank you for the session. Have a productive day ahead.")
                break
            
            if not user_input.strip():
                continue

            messages.append({"role": "user", "content": user_input})
            print("\nCoach is thinking...")

            if args.provider == "claude":
                api_messages = [m for m in messages if m["role"] != "system"]
                response = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=SYSTEM_INSTRUCTION,
                    messages=api_messages,
                )
                reply = response.content[0].text
            else:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.7,
                )
                reply = response.choices[0].message.content

            print(f"\nCoach: {reply}")
            messages.append({"role": "assistant", "content": reply})
            
        except KeyboardInterrupt:
            print("\n\nSession interrupted. Exiting...")
            break
        except Exception as e:
            print(f"\nAn error occurred communicating with the AI: {e}")

if __name__ == "__main__":
    main()
