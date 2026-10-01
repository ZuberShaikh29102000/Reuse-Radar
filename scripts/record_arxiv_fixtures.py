"""Record real arXiv LaTeX sources as filter-test fixtures. Run manually.

    python scripts/record_arxiv_fixtures.py

Writes tests/fixtures/arxiv/<arxiv_id>/<file>.tex. ATLAS author-list files are left out: they
follow the bibliography (outside anything the filter reads) and are ~250 KB of personal names.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from reuse_radar.clients.arxiv import ArxivClient
from reuse_radar.config import Settings

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "arxiv"

# Three contrasting ATLAS papers: a 2021 search setting limits, a 2024 inclusive+differential
# cross-section measurement, and a 2024 differential measurement aimed at reinterpretation.
PAPERS = ("2112.11876", "2401.05299", "2403.02793")
EXCLUDED = ("atlas_authlist.tex",)


def main() -> None:
    settings = Settings.from_env()
    with tempfile.TemporaryDirectory() as tmp:
        client = ArxivClient(contact_email=settings.inspire_contact_email, cache_dir=Path(tmp))
        for arxiv_id in PAPERS:
            source = client.get_source(arxiv_id)
            out = FIXTURE_DIR / arxiv_id
            for name, text in source.tex_files.items():
                if Path(name).name in EXCLUDED:
                    continue
                path = out / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8", newline="")
            print(f"{arxiv_id}: {source.kind}, {sorted(source.tex_files)}")


if __name__ == "__main__":
    main()
