"""MCP server tests, end to end: MCP tool -> ApiClient -> (in-process) Django API -> database."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import httpx
import pytest
from django.test import Client, override_settings
from mcp.server.mcpserver.exceptions import ToolError

from reuse_radar.api.models import DeclaredProduct, Gap, Paper
from reuse_radar.mcp.server import ApiClient, build_server

# transaction=True: the MCP SDK runs tools on a worker thread with its own database connection,
# which cannot see rows inside the default test transaction.
pytestmark = pytest.mark.django_db(transaction=True)
API = override_settings(ALLOWED_HOSTS=["testserver"], SECURE_SSL_REDIRECT=False)


def _django_transport() -> httpx.MockTransport:
    """Route the MCP server's HTTP calls into the Django test client (no network)."""

    def handler(request: httpx.Request) -> httpx.Response:
        with API:
            response = Client().get(request.url.path, dict(request.url.params))
        return httpx.Response(response.status_code, content=response.content)

    return httpx.MockTransport(handler)


@pytest.fixture
def server() -> Any:
    paper = Paper.objects.create(
        inspire_id=2745375,
        arxiv_id="2401.05299",
        title="ttW cross-sections",
        earliest_date=dt.date(2024, 1, 10),
        hepdata_record_id=149762,
        readiness_score=17.4,
        processing_status="reconciled",
    )
    for i, (ptype, status, severity) in enumerate(
        [("cross_section", "missing", 2), ("likelihood", "uncertain", 2), ("other", "published", 0)]
    ):
        product = DeclaredProduct.objects.create(
            paper=paper,
            product_type=ptype,
            description=f"{ptype} product",
            evidence_span=f"Evidence sentence number {i} for the {ptype} result.",
            evidence_section="Results",
            confidence=0.9,
            embedding=[float(i == j) for j in range(384)],
            fingerprint=f"fp{i}",
            extraction_version="v1",
        )
        Gap.objects.create(
            declared_product=product, status=status, severity=severity, reconcile_version="1"
        )
    client = ApiClient("http://testserver", http=httpx.Client(transport=_django_transport()))
    return build_server(client)


def call(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    result = asyncio.run(server.call_tool(name, arguments))
    return result


def payload(result: Any) -> dict[str, Any]:
    if getattr(result, "structuredContent", None) is not None:
        data: dict[str, Any] = result.structuredContent
        return data
    parsed: dict[str, Any] = json.loads(result.content[0].text)
    return parsed


def test_tools_are_listed_and_read_only(server: Any) -> None:
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) == {"find_reuse_gaps", "get_reuse_profile"}
    for tool in tools.values():
        assert tool.annotations.read_only_hint is True
        assert tool.description
    assert "likelihood" in tools["find_reuse_gaps"].description


def test_find_reuse_gaps_returns_open_gaps_with_evidence(server: Any) -> None:
    data = payload(call(server, "find_reuse_gaps", {}))
    assert data["total_matching"] == 2
    statuses = [g["status"] for g in data["gaps"]]
    assert "published" not in statuses
    first = data["gaps"][0]
    assert first["evidence"].startswith("Evidence sentence")
    assert first["paper"]["inspire_url"] == "https://inspirehep.net/literature/2745375"


def test_find_reuse_gaps_filters(server: Any) -> None:
    data = payload(call(server, "find_reuse_gaps", {"product_type": "likelihood", "year": 2024}))
    assert [g["product_type"] for g in data["gaps"]] == ["likelihood"]
    data = payload(call(server, "find_reuse_gaps", {"status": "published"}))
    assert [g["status"] for g in data["gaps"]] == ["published"]


def test_get_reuse_profile_summarises_a_paper(server: Any) -> None:
    data = payload(call(server, "get_reuse_profile", {"inspire_id": 2745375}))
    assert data["paper"]["readiness_score"] == 17.4
    assert data["products_by_status"] == {"missing": 1, "uncertain": 1, "published": 1}
    assert len(data["products"]) == 3


def test_errors_are_reported_to_the_agent(server: Any) -> None:
    """ToolError is what the MCP protocol layer turns into an isError result for the agent."""
    with pytest.raises(ToolError, match="not found"):
        call(server, "get_reuse_profile", {"inspire_id": 1})
    with pytest.raises(ToolError, match="invalid arguments"):
        call(server, "find_reuse_gaps", {"product_type": "banana"})
    with pytest.raises(ToolError, match="limit"):
        call(server, "find_reuse_gaps", {"limit": 500})


def test_min_severity_filters_only_when_given(server: Any) -> None:
    """Regression: a default of 1 hid every published product (severity 0)."""
    assert payload(call(server, "find_reuse_gaps", {"min_severity": 3}))["total_matching"] == 0
    assert payload(call(server, "find_reuse_gaps", {"min_severity": 2}))["total_matching"] == 2
