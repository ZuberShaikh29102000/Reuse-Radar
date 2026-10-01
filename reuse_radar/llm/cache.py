"""Disk cache for LLM responses, keyed by sha256(provider + model + prompt). SPEC section 2, item 4.

"prompt" is the canonical JSON of everything that shapes the output: system and user text,
response schema and generation parameters. Changing any of them is a different prompt, and so a
different key; that is what makes the schema-failure retry (which adds a corrective message)
a fresh call rather than a replay of the bad response.

Entries never expire: a given prompt to a given model version is answered once. Reruns cost zero
API calls.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CachedResponse:
    provider: str
    model: str
    text: str
    input_tokens: int
    output_tokens: int  # includes reasoning/thinking tokens: they count against quotas too
    created_at: float


def canonical_prompt(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def cache_key(provider: str, model: str, prompt: str) -> str:
    # Separators keep ("ab", "c") and ("a", "bc") distinct.
    return hashlib.sha256(f"{provider}\x00{model}\x00{prompt}".encode()).hexdigest()


class LLMCache:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        return self._root / key[:2] / f"{key}.json"

    def get(self, provider: str, model: str, prompt: str) -> CachedResponse | None:
        key = cache_key(provider, model, prompt)
        path = self._path(key)
        if not path.exists():
            return None
        entry = json.loads(path.read_text(encoding="utf-8"))
        if entry.get("key") != key or entry.get("prompt") != prompt:
            raise RuntimeError(f"LLM cache entry {path} does not match its key")
        return CachedResponse(**entry["response"])

    def set(self, prompt: str, response: CachedResponse) -> None:
        key = cache_key(response.provider, response.model, prompt)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        # The prompt is stored alongside the response so cached runs can be audited.
        tmp.write_text(
            json.dumps({"key": key, "prompt": prompt, "response": asdict(response)}),
            encoding="utf-8",
        )
        os.replace(tmp, path)
