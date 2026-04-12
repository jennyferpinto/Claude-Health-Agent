"""Download the latest MacroFactor xlsx export from the Notion database.

Uses the Notion public REST API (api.notion.com) directly rather than an MCP.
The Anthropic Notion MCP returns opaque `file://` references for file blocks,
which can't be downloaded. The real API returns signed S3 URLs which can.

Requires `NOTION_API_KEY` — a secret from an internal Notion integration that
has been explicitly shared with the "MacroFactor Exports" database via
Notion's Share -> Connections dialog.
"""

import os
import sys
from pathlib import Path

import requests

NOTION_VERSION = "2022-06-28"
DATABASE_ID = "33ff1837-fa88-8107-a240-d7d2bfcd87cf"  # MacroFactor Exports
DEFAULT_OUTPUT = "/tmp/macrofactor.xlsx"


def main() -> int:
    token = os.environ.get("NOTION_API_KEY")
    if not token:
        print("error: NOTION_API_KEY is not set", file=sys.stderr)
        return 1

    output_path = Path(os.environ.get("MACROFACTOR_XLSX_PATH", DEFAULT_OUTPUT))
    auth = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION}

    # 1. Query the database for the most recently created row.
    r = requests.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        headers={**auth, "Content-Type": "application/json"},
        json={
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
            "page_size": 1,
        },
        timeout=30,
    )
    if r.status_code != 200:
        print(f"error: notion query failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    results = r.json().get("results", [])
    if not results:
        print("error: MacroFactor Exports database is empty", file=sys.stderr)
        return 1
    latest = results[0]
    page_id = latest["id"]
    created = latest.get("created_time", "<unknown>")
    print(f"latest export row: {page_id} (created {created})")

    # 2. List the blocks on that row's page and find the first file block.
    r = requests.get(
        f"https://api.notion.com/v1/blocks/{page_id}/children",
        headers=auth,
        params={"page_size": 100},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"error: block list failed: {r.status_code} {r.text}", file=sys.stderr)
        return 1
    blocks = r.json().get("results", [])
    file_block = next((b for b in blocks if b.get("type") == "file"), None)
    if file_block is None:
        types = [b.get("type") for b in blocks]
        print(f"error: no file block on page {page_id}. block types: {types}", file=sys.stderr)
        return 1

    payload = file_block["file"]
    if payload.get("type") == "file":
        signed_url = payload["file"]["url"]
    elif payload.get("type") == "external":
        signed_url = payload["external"]["url"]
    else:
        print(f"error: unexpected file payload type {payload.get('type')}", file=sys.stderr)
        return 1

    # 3. Download the xlsx.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(signed_url, timeout=60)
    if r.status_code != 200:
        print(f"error: download failed: {r.status_code}", file=sys.stderr)
        return 1
    output_path.write_bytes(r.content)
    print(f"wrote {len(r.content)} bytes to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
