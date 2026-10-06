import os
from pathlib import Path
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")

# Which provider to use: "nvidia" or "openrouter"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "nvidia").lower()

SESSIONS_DIR = ROOT / "data" / "sessions"

def load_models() -> dict:
    with open(ROOT / "models.yaml") as f:
        return yaml.safe_load(f)

MODELS = load_models()