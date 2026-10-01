"""Record real DataCite responses for HEPData records as test fixtures. Run manually.

    python scripts/record_datacite_fixtures.py

Uses the live API through HepDataClient (so the recorded URLs are exactly the ones the client
builds) and copies the responses from the client's cache into tests/fixtures/datacite/.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from reuse_radar.clients.hepdata import HepDataClient
from reuse_radar.config import Settings

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "datacite"
# 2745375: tables plus HS3 likelihood resources. 1995886: 31 tables, several record versions.
# 1: an INSPIRE id with no HEPData record.
INSPIRE_IDS = (2745375, 1995886, 1)


def main() -> None:
    settings = Settings.from_env()
    with tempfile.TemporaryDirectory() as tmp:
        client = HepDataClient(contact_email=settings.inspire_contact_email, cache_dir=Path(tmp))
        for inspire_id in INSPIRE_IDS:
            record = client.find_record(inspire_id)
            if record is not None:
                print(inspire_id, record.record_doi, len(client.list_tables(record)), "parts")
            else:
                print(inspire_id, "no record")
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, str] = {}
        for i, path in enumerate(sorted(Path(tmp, "datacite").rglob("*.json"))):
            entry = json.loads(path.read_text(encoding="utf-8"))
            name = f"response_{i:02d}.json"
            (FIXTURE_DIR / name).write_text(json.dumps(entry["body"], indent=1), encoding="utf-8")
            manifest[entry["url"]] = name
    (FIXTURE_DIR / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print("recorded", len(manifest), "responses")


if __name__ == "__main__":
    main()
