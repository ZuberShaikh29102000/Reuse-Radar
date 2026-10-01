"""Record live INSPIRE responses as test fixtures. Run manually; tests never touch the network.

    INSPIRE_CONTACT_EMAIL=you@example.org python scripts/record_inspire_fixtures.py

Writes tests/fixtures/inspire/<n>.json plus manifest.json mapping request URL -> file.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx

from reuse_radar.clients.inspire import InspireClient, QueryTooLargeError
from reuse_radar.config import Settings
from reuse_radar.pipeline.harvest import FIELDS, CorpusConfig

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "inspire"

# Small page size so the one-year slice spans more than one page and exercises links.next.
SLICE_YEAR = 2021
SLICE_PAGE_SIZE = 40
# A query known to exceed 10,000 records, for the fail-loudly test.
OVERSIZE_QUERY = "t higgs"


class RecordingTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self._inner = httpx.HTTPTransport()
        self.recorded: dict[str, dict[str, object]] = {}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        response.read()
        if response.status_code == 200:
            self.recorded[str(request.url)] = response.json()
        return response


def main() -> None:
    settings = Settings.from_env()
    transport = RecordingTransport()
    with tempfile.TemporaryDirectory() as tmp:
        client = InspireClient(
            contact_email=settings.inspire_contact_email,
            cache_dir=Path(tmp),
            http=httpx.Client(transport=transport, timeout=30.0),
            use_cache=False,
        )
        corpus = CorpusConfig.from_toml(Path("config/corpus.toml"))
        hits = list(
            client.search_literature(
                corpus.slice_query(SLICE_YEAR), fields=FIELDS, page_size=SLICE_PAGE_SIZE
            )
        )
        print(f"slice {SLICE_YEAR}: {len(hits)} hits")
        try:
            list(client.search_literature(OVERSIZE_QUERY, fields=("control_number",), page_size=1))
        except QueryTooLargeError as exc:
            print(f"oversize query raised as expected: {exc}")

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for i, (url, body) in enumerate(transport.recorded.items()):
        name = f"response_{i:02d}.json"
        (FIXTURE_DIR / name).write_text(
            json.dumps(body, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        manifest[url] = name
    (FIXTURE_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(manifest)} fixtures to {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
