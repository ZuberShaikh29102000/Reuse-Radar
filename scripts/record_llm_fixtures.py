"""Record raw Groq and Gemini HTTP responses as test fixtures. Run manually (uses a few hundred
tokens of free quota). Tests replay these bodies; they never call the APIs.

    python scripts/record_llm_fixtures.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

from reuse_radar.llm.router import GEMINI_URL, GROQ_URL, GeminiProvider, GroqProvider, LLMRequest
from reuse_radar.llm.schemas import EXTRACTION_JSON_SCHEMA

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "llm"
PASSAGE = (
    "### Passage 1 (caption; section: Results)\n"
    "\\caption{Observed and expected limits at 95\\% CL on the cross section of nonresonant "
    "Higgs boson pair production as a function of $\\kappa_\\lambda$.}"
)


def main() -> None:
    load_dotenv(Path.cwd() / ".env")
    request = LLMRequest(
        system="List the reusable data products in the passages. Copy evidence spans exactly.",
        messages=(("user", PASSAGE),),
        schema_name="declared_products",
        json_schema=EXTRACTION_JSON_SCHEMA,
        max_output_tokens=1500,
    )
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    groq = GroqProvider(os.environ["GROQ_API_KEY"])
    r = httpx.post(
        GROQ_URL,
        json=groq.payload(request),
        headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    body.pop("x_groq", None)  # request ids are noise
    (FIXTURE_DIR / "groq_ok.json").write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")

    gemini = GeminiProvider(os.environ["GEMINI_API_KEY"])
    payload = {k: v for k, v in gemini.payload(request).items() if k != "model"}
    r = httpx.post(
        GEMINI_URL.format(model=gemini.model),
        json=payload,
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        timeout=180,
    )
    r.raise_for_status()
    body = r.json()
    body.pop("responseId", None)
    (FIXTURE_DIR / "gemini_ok.json").write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
    print("recorded", sorted(p.name for p in FIXTURE_DIR.iterdir()))


if __name__ == "__main__":
    main()
