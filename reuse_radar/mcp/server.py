"""MCP server exposing Reuse Radar to AI agents (SPEC Phase 6).

Tools:
- find_reuse_gaps: the triage queue, filtered: which declared data products are missing
  from HEPData.
- get_reuse_profile: one paper's declared products and their HEPData status.

The server is a thin client of the public REST API (REUSE_RADAR_API_URL, default the local dev
server). It holds no database credentials and calls no model: answers are the pipeline's
precomputed rows, exactly as the API serves them.

Run (stdio, for local agent clients):
    REUSE_RADAR_API_URL=https://<service>.onrender.com python -m reuse_radar.mcp.server
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from reuse_radar import __version__

DEFAULT_API_URL = "http://127.0.0.1:8000"
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)


class ApiClient:
    def __init__(self, base_url: str, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=30.0)
        self._http.base_url = httpx.URL(base_url.rstrip("/"))
        self._http.headers["User-Agent"] = f"ReuseRadar-MCP/{__version__}"

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = self._http.get(path, params=clean)
        except httpx.TransportError as exc:
            raise ToolError(f"Reuse Radar API unreachable: {exc}") from exc
        if response.status_code == 404:
            raise ToolError("not found in Reuse Radar")
        if response.status_code == 400:
            raise ToolError(f"invalid arguments: {response.text[:300]}")
        if response.status_code != 200:
            raise ToolError(f"Reuse Radar API returned {response.status_code}")
        return response.json()


def _paper(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "inspire_id": p["inspire_id"],
        "title": p["title"],
        "year": p["year"],
        "readiness_score": p["readiness_score"],
        "inspire_url": p["inspire_url"],
        "arxiv_url": p["arxiv_url"],
        "hepdata_url": p["hepdata_url"],
    }


def _gap(g: dict[str, Any], *, with_paper: bool) -> dict[str, Any]:
    product = g["product"]
    out: dict[str, Any] = {
        "status": g["status"],
        "severity": g["severity"],
        "product_type": product["product_type"],
        "description": product["description"],
        "evidence": " ".join(product["evidence_span"].split())[:400],
        "evidence_section": product["evidence_section"],
        "confidence": product["confidence"],
        "matched_hepdata_table": (
            {"name": g["matched_table"]["name"], "doi_url": g["matched_table"]["doi_url"]}
            if g.get("matched_table")
            else None
        ),
        "latest_review": product.get("latest_review"),
    }
    if with_paper:
        out["paper"] = _paper(g["paper"])
    return out


def build_server(client: ApiClient) -> MCPServer:
    server = MCPServer(
        name="reuse-radar",
        version=__version__,
        instructions=(
            "Reuse Radar lists reusable data products (cross-sections, limits, likelihoods, "
            "covariance matrices, ...) declared in ATLAS papers and whether each is on HEPData. "
            "Status meanings: missing = the paper has a HEPData record but not this product; "
            "no_record = the paper has no HEPData record; uncertain = a plausible match exists, "
            "needs a human; published = found on HEPData. Severity 3 is most urgent. "
            "Every product carries a verbatim evidence quote from the paper."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    def find_reuse_gaps(
        product_type: str | None = None,
        year: int | None = None,
        min_severity: int | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Find declared data products that are missing from HEPData, most severe first.

        Args:
            product_type: one or more, comma-separated, of cross_section, upper_limit,
                efficiency_map, likelihood, covariance_matrix, acceptance_table, cutflow,
                correlation_matrix, other. Omit for all types.
            year: paper year (earliest date), e.g. 2024.
            min_severity: 1 to 3 to keep only more urgent gaps (3 = most urgent). Omit for
                no severity filter; published products have severity 0.
            status: comma-separated statuses; default the open ones (missing, uncertain,
                no_record). Use "published" to see what is already on HEPData.
            limit: number of results, 1 to 100.
        """
        if not 1 <= limit <= 100:
            raise ToolError("limit must be between 1 and 100")
        data = client.get(
            "/api/gaps",
            {
                "product_type": product_type,
                "year": year,
                "min_severity": min_severity,
                "status": status,
                "page_size": limit,
            },
        )
        return {
            "total_matching": data["count"],
            "returned": len(data["results"]),
            "gaps": [_gap(g, with_paper=True) for g in data["results"]],
        }

    @server.tool(annotations=READ_ONLY)
    def get_reuse_profile(inspire_id: int) -> dict[str, Any]:
        """Reuse profile of one paper: every declared data product and its HEPData status.

        Args:
            inspire_id: the paper's INSPIRE-HEP record number (e.g. 2745375).
        """
        data = client.get(f"/api/papers/{inspire_id}/gaps")
        gaps = data["gaps"]
        counts: dict[str, int] = {}
        for g in gaps:
            counts[g["status"]] = counts.get(g["status"], 0) + 1
        return {
            "paper": {
                **_paper(data["paper"]),
                "processing_status": data["paper"]["processing_status"],
            },
            "products_by_status": counts,
            "products": [_gap(g, with_paper=False) for g in gaps],
        }

    return server


def main() -> None:
    api_url = os.environ.get("REUSE_RADAR_API_URL", DEFAULT_API_URL)
    build_server(ApiClient(api_url)).run("stdio")


if __name__ == "__main__":
    main()
