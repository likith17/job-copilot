import os
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("ANTHROPIC_API_KEY")

if key and key.startswith("sk-ant-"):
    print("Key loaded successfully (length:", len(key), "characters)")
else:
    print("Key not found — check your .env file")