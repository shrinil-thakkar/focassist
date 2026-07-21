"""Optional LLM-result cache, keyed by (email id + clean_body prefix).

Makes re-runs cheaper and makes the LLM layer idempotent too, not just the
rule layer: an unchanged email hits the cache instead of the model again.
Pure file I/O, no network.
"""

import hashlib
import json
import os
from pathlib import Path

# Same FOCASSIST_DIR convention as credentials/token/db (agent/google/auth.py).
_DIR = Path(os.environ.get("FOCASSIST_DIR", Path.home() / ".focassist"))
DEFAULT_CACHE_PATH = str(_DIR / "label_llm_cache.json")
KEY_BODY_CHARS = 500


def cache_key(email: dict) -> str:
    body_prefix = (email.get("clean_body") or "")[:KEY_BODY_CHARS]
    raw = f"{email.get('id', '')}:{body_prefix}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_cache(path: str = DEFAULT_CACHE_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_cache(cache: dict, path: str = DEFAULT_CACHE_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=2))
