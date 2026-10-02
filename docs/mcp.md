# Reuse Radar MCP server

Lets AI agents ask Reuse Radar which data products are missing from HEPData. It is a thin,
read-only client of the public API: no database access, no model calls.

## Tools

| Tool | Arguments | Returns |
|---|---|---|
| `find_reuse_gaps` | `product_type` (comma list), `year`, `min_severity` (1–3), `status` (comma list; default open gaps), `limit` (1–100) | Gaps, most severe first. Each has its evidence quote, paper links, matched HEPData table and latest curator review. |
| `get_reuse_profile` | `inspire_id` | One paper: readiness score, counts by status, and every declared product with its status. |

Status meanings:

| Status | Meaning |
|---|---|
| `missing` | The paper has a HEPData record, but not this product. |
| `no_record` | The paper has no HEPData record at all. |
| `uncertain` | A plausible match exists; a human should decide. |
| `published` | Found on HEPData. |

## Running it

```sh
# against the deployed API
REUSE_RADAR_API_URL=https://<service>.onrender.com uv run python -m reuse_radar.mcp.server
# against a local API (python manage.py runserver)
REUSE_RADAR_API_URL=http://127.0.0.1:8000 uv run python -m reuse_radar.mcp.server
```

It speaks MCP over stdio. To add it to an MCP client, for example Claude Desktop's
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "reuse-radar": {
      "command": "uv",
      "args": ["--directory", "/path/to/reuse-radar", "run", "python", "-m", "reuse_radar.mcp.server"],
      "env": {"REUSE_RADAR_API_URL": "https://<service>.onrender.com"}
    }
  }
}
```

Example questions an agent can then answer:

- "Which 2024 ATLAS papers declare likelihoods that are not on HEPData?"
- "What does arXiv:2401.05299 publish on HEPData, and what is missing?"

## Notes

- Built on the MCP Python SDK 2.x (`MCPServer`). Both tools are annotated `read_only_hint=true`.
- Errors (unknown paper, invalid filter, API unreachable) come back to the agent as tool errors
  with a readable message.
- Verified live on 2026-10-02 over stdio against the local API: tool listing, both tools, and
  the error path.
