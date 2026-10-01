from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from reuse_radar.clients.inspire import InspireClient
from reuse_radar.pipeline.harvest import (
    CorpusConfig,
    HarvestError,
    harvest,
    harvest_year,
    parse_hit,
)

from .conftest import ReplayTransport
from .test_inspire_client import _fixture_body

MakeClient = Callable[..., tuple[InspireClient, ReplayTransport]]
CORPUS = CorpusConfig(
    name="atlas-test", query="collaboration ATLAS and tc published", start_year=2021, end_year=2021
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_harvest_writes_one_sorted_jsonl_per_year(make_client: MakeClient, tmp_path: Path) -> None:
    client, _ = make_client()
    paths = harvest(client, CORPUS, tmp_path / "data", page_size=40)

    path = paths[2021]
    assert path == tmp_path / "data" / "harvest" / "atlas-test" / "2021.jsonl"
    rows = _read_jsonl(path)
    assert len(rows) == 67
    ids = [r["inspire_id"] for r in rows]
    assert ids == sorted(ids)
    assert all(r["earliest_date"].startswith("2021") for r in rows)
    assert set(rows[0]) == {
        "inspire_id",
        "arxiv_id",
        "title",
        "collaborations",
        "earliest_date",
        "inspire_links_hepdata",
    }
    assert sum(r["inspire_links_hepdata"] for r in rows) > 0
    # INSPIRE's `collaboration ATLAS` also matches e.g. "ATLAS ITk"; the harvester keeps
    # exactly what the configured query returns.
    assert all(any("ATLAS" in c for c in r["collaborations"]) for r in rows)
    assert sum(r["arxiv_id"] is None for r in rows) == 9


def test_harvest_is_idempotent(make_client: MakeClient, tmp_path: Path) -> None:
    client, transport = make_client()
    path = harvest_year(client, CORPUS, 2021, tmp_path, page_size=40)
    first = path.read_bytes()
    harvest_year(client, CORPUS, 2021, tmp_path, page_size=40)
    assert path.read_bytes() == first
    assert len(transport.requests) == 2  # rerun hit the cache only


def test_slice_query_uses_earliest_date() -> None:
    assert CORPUS.slice_query(2021) == "collaboration ATLAS and tc published and de 2021"


def test_record_outside_slice_year_raises(make_client: MakeClient, tmp_path: Path) -> None:
    def shift_date(request: httpx.Request) -> httpx.Response:
        body = _fixture_body(request)
        body["hits"]["hits"][0]["metadata"]["earliest_date"] = "2019-05-01"
        return httpx.Response(200, json=body)

    client, _ = make_client(shift_date)
    with pytest.raises(HarvestError, match="slice 2021"):
        harvest_year(client, CORPUS, 2021, tmp_path, page_size=40)
    assert not (tmp_path / "2021.jsonl").exists()


def test_existing_output_survives_a_failed_rerun(make_client: MakeClient, tmp_path: Path) -> None:
    client, _ = make_client()
    path = harvest_year(client, CORPUS, 2021, tmp_path, page_size=40)
    good = path.read_bytes()

    broken, _ = make_client(lambda r: httpx.Response(500), use_cache=False, max_retries=0)
    with pytest.raises(Exception, match="500"):
        harvest_year(broken, CORPUS, 2021, tmp_path, page_size=40)
    assert path.read_bytes() == good


@pytest.mark.parametrize("field", ["control_number", "titles", "earliest_date"])
def test_missing_required_field_raises(field: str) -> None:
    metadata: dict[str, Any] = {
        "control_number": 1,
        "titles": [{"title": "T"}],
        "earliest_date": "2021-01-01",
    }
    del metadata[field]
    hit = {"id": "1", "metadata": metadata}
    with pytest.raises(HarvestError):
        parse_hit(hit)


def test_missing_arxiv_id_is_allowed() -> None:
    paper = parse_hit(
        {"metadata": {"control_number": 1, "titles": [{"title": "T"}], "earliest_date": "2021"}}
    )
    assert paper.arxiv_id is None
    assert paper.collaborations == []


def test_corpus_config_loads_repo_file() -> None:
    corpus = CorpusConfig.from_toml(Path(__file__).parent.parent / "config" / "corpus.toml")
    assert list(corpus.years) == [2020, 2021, 2022, 2023, 2024, 2025]
