#!/usr/bin/env python3
"""
Export OpenSearch docs to:
  1) A summary CSV (original behavior unchanged)
  2) Per-doc JSON files using _id as <_id>.json
  3) A second CSV listing every created JSON file

Env:
  OPENSEARCH_USERNAME (default: admin)
  OPENSEARCH_PASSWORD (default: admin)
  OPENSEARCH_HOSTNAME (default: opensearch-node)

Requires:
  - Python 3.11+
  - opensearch-py==2.8.0
  - python-dotenv
"""

import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.exceptions import TransportError, ConnectionError  # type: ignore
import urllib3

# Suppress SSL warnings since verify_certs=False is required
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------- Configuration / constants ----------
DEFAULT_INDEX_PATTERN = "pyspider-mk-gpn-2025-*"
SCROLL_KEEPALIVE = "2m"
SCROLL_SIZE = 1000

# Original summary CSV columns (unchanged)
CSV_COLUMNS = ["source", "url", "final_url", "hostname", "domain", "http_status", "url_mismatch", "url_path"]

# New JSON export inventory CSV columns
JSON_INV_COLUMNS = [
    "id", "index", "json_file", "json_bytes",
    "url", "final_url", "hostname", "domain", "http_status", "url_path"
]

# ---------- Helpers ----------

def build_query(start_date: str, end_date: str, hostname: Optional[str] = None, url_path_pattern: Optional[str] = None) -> Dict[str, Any]:
    """
    Build a bool query combining:
      - range on 'creation_date' (inclusive day bounds)
      - optional exact-match term on 'hostname' if provided
      - optional wildcard match on 'url_path' if provided
    """
    gte = f"{start_date}T00:00:00Z"
    lte = f"{end_date}T23:59:59Z"

    must_clauses: List[Dict[str, Any]] = [
        {"range": {"creation_date": {"gte": gte, "lte": lte}}}
    ]

    if hostname:
        must_clauses.append({"term": {"hostname": hostname}})
    
    if url_path_pattern:
        # Use wildcard query for pattern matching
        must_clauses.append({"wildcard": {"url_path": url_path_pattern}})

    return {"query": {"bool": {"must": must_clauses}}}

def ensure_output_dir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export OpenSearch docs to CSV + per-doc JSON via Scroll API.")
    parser.add_argument("--index-pattern", default=DEFAULT_INDEX_PATTERN, help="Index pattern to search.")
    parser.add_argument("--start-date", required=True, help="Start date (YYYY-MM-DD).")
    parser.add_argument("--end-date", required=True, help="End date (YYYY-MM-DD).")
    parser.add_argument("--hostname", help="Optional: exact hostname filter (e.g., www.contoso.com).")
    parser.add_argument("--url-path", help="Optional: URL path pattern filter (e.g., /resources/blog/* for wildcard matching).")
    parser.add_argument("--output-dir", default="output", help="Directory to write CSV files.")
    parser.add_argument("--scroll-size", type=int, default=SCROLL_SIZE, help="Scroll page size (default 1000).")
    parser.add_argument("--timeout", default=SCROLL_KEEPALIVE, help="Scroll keepalive (e.g., 2m).")

    # JSON export path + inventory CSV name
    parser.add_argument("--json-dir", default="output/json", help="Directory where <_id>.json files will be written.")
    parser.add_argument("--json-csv-name", default=None,
                        help="Optional override for JSON inventory CSV filename. Default: json_exports_<YYYY-MM-DD>.csv")
    parser.add_argument("--skip-existing", action="store_true",
                        help="If set, do not overwrite existing JSON files.")
    parser.add_argument("--pretty-print", action="store_true",
                        help="If set, pretty-print JSON files with indentation.")
    parser.add_argument("--include-metadata", action="store_true",
                        help="If set, include document metadata (_id, _index, _score, etc.) in JSON export.")
    parser.add_argument("--replace-text", action="store_true",
                        help="If set, replace 'Heartland' with 'Auris' and 'Heartland's' with 'Auris'' in exported data.")

    return parser.parse_args()

def load_env() -> None:
    # Load from .env if present; system env vars still take precedence when set
    load_dotenv(override=False)

# ---------- Connection ----------

def connect_opensearch() -> Optional[OpenSearch]:
    """
    Attempt to create and return an OpenSearch client.
    If the connection fails, handle the exception and return None.
    """
    OS_USERNAME = os.getenv("OPENSEARCH_USERNAME", "admin")
    OS_PASSWORD = os.getenv("OPENSEARCH_PASSWORD", "admin")
    OS_HOSTNAME = os.getenv("OPENSEARCH_HOSTNAME", "opensearch-node")

    OPENSEARCH_HOST = f"https://{OS_HOSTNAME}:9200"

    try:
        client = OpenSearch(
            hosts=[OPENSEARCH_HOST],
            http_auth=(OS_USERNAME, OS_PASSWORD),
            use_ssl=True,
            verify_certs=False,
            connection_class=RequestsHttpConnection,
            ssl_show_warn=False,
        )
        if not client.ping():
            print("Connected but ping failed. Check credentials or endpoint.", file=sys.stderr)
            return None
        return client
    except ConnectionError as e:
        print("Connection error while creating OpenSearch client:", e, file=sys.stderr)
        return None

# ---------- Export helpers ----------

def flatten_hit_for_summary(hit: Dict[str, Any]) -> Dict[str, Any]:
    """Flat row for the original summary CSV."""
    source = hit.get("_source", {}) or {}
    url = source.get("url", "") or ""
    final_url = source.get("final_url", "") or ""
    return {
        "source": hit.get("_index", ""),
        "url": url,
        "final_url": final_url,
        "hostname": source.get("hostname", ""),
        "domain": source.get("domain", ""),
        "http_status": source.get("http_status", ""),
        "url_mismatch": "Yes" if url != final_url else "No",
        "url_path": source.get("url_path", ""),
    }

def replace_heartland_text(obj: Any) -> Any:
    """
    Recursively replace 'Heartland' with 'Auris' and 'Heartland's' with 'Auris'' 
    in strings within dictionaries, lists, and string values.
    """
    if isinstance(obj, dict):
        return {k: replace_heartland_text(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [replace_heartland_text(item) for item in obj]
    elif isinstance(obj, str):
        # Replace Heartland's first (more specific pattern)
        result = re.sub(r"Heartland's", "Auris'", obj, flags=re.IGNORECASE)
        # Then replace standalone Heartland
        result = re.sub(r"Heartland", "Auris", result, flags=re.IGNORECASE)
        return result
    else:
        return obj

def save_json_for_hit(
    hit: Dict[str, Any],
    json_dir: str,
    skip_existing: bool = False,
    pretty_print: bool = False,
    include_metadata: bool = False,
    replace_text: bool = False,
) -> Tuple[Optional[str], int]:
    """
    Save the hit to <json_dir>/<id>.json.
    Returns (filepath or None if not saved, bytes_written).
    """
    _id = hit.get("_id")
    if not _id:
        return (None, 0)

    # Prepare the data to export
    if include_metadata:
        # Export the entire hit including metadata
        export_data = hit
    else:
        # Export only the _source data
        export_data = hit.get("_source", {})

    if not export_data:
        return (None, 0)

    # Apply text replacement if requested
    if replace_text:
        export_data = replace_heartland_text(export_data)

    ensure_output_dir(json_dir)
    out_path = os.path.join(json_dir, f"{_id}.json")

    if skip_existing and os.path.exists(out_path):
        try:
            return (out_path, os.path.getsize(out_path))
        except OSError:
            # If stat fails, proceed to write
            pass

    try:
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            if pretty_print:
                json.dump(export_data, f, ensure_ascii=False, indent=2, sort_keys=True)
            else:
                json.dump(export_data, f, ensure_ascii=False)
        size = os.path.getsize(out_path)
        return (out_path, size)
    except Exception as e:
        print(f"Failed to write JSON for id={_id}: {e}", file=sys.stderr)
        return (None, 0)

def flatten_hit_for_json_inventory(
    hit: Dict[str, Any],
    json_file: Optional[str],
    json_bytes: int,
) -> Dict[str, Any]:
    """Flat row for the JSON export inventory CSV."""
    source = hit.get("_source", {}) or {}
    return {
        "id": hit.get("_id", ""),
        "index": hit.get("_index", ""),
        "json_file": json_file or "",
        "json_bytes": json_bytes,
        "url": source.get("url", ""),
        "final_url": source.get("final_url", ""),
        "hostname": source.get("hostname", ""),
        "domain": source.get("domain", ""),
        "http_status": source.get("http_status", ""),
        "url_path": source.get("url_path", ""),
    }

# ---------- Export logic ----------

def export_with_scroll(
    client: OpenSearch,
    index_pattern: str,
    body: Dict[str, Any],
    summary_csv_path: str,
    json_csv_path: str,
    json_dir: str,
    scroll_size: int,
    scroll_keepalive: str,
    skip_existing: bool,
    pretty_print: bool,
    include_metadata: bool,
    replace_text: bool,
) -> Tuple[int, int]:
    """
    Execute a scroll search, stream results to:
      - summary CSV (original behavior)
      - per-file JSON inventory CSV
      - write JSON files to json_dir
    Returns (total_docs_streamed, total_json_files_written_or_skipped).
    """
    total_docs = 0
    total_json_rows = 0

    print(f"[{datetime.datetime.utcnow().isoformat()}Z] Starting initial search...")
    try:
        # Remove _source_includes to get all fields
        page = client.search(
            index=index_pattern,
            body=body,
            size=scroll_size,
            scroll=scroll_keepalive,
        )
    except TransportError as e:
        print(f"Initial search failed: {e}", file=sys.stderr)
        return (0, 0)

    scroll_id = page.get("_scroll_id")
    hits = page.get("hits", {}).get("hits", [])

    # Ensure dirs
    ensure_output_dir(os.path.dirname(summary_csv_path) or ".")
    ensure_output_dir(os.path.dirname(json_csv_path) or ".")
    ensure_output_dir(json_dir)

    with open(summary_csv_path, "w", newline="", encoding="utf-8") as summary_f, \
         open(json_csv_path, "w", newline="", encoding="utf-8") as json_f:

        summary_writer = csv.DictWriter(summary_f, fieldnames=CSV_COLUMNS)
        summary_writer.writeheader()

        json_writer = csv.DictWriter(json_f, fieldnames=JSON_INV_COLUMNS)
        json_writer.writeheader()

        # write first page
        for h in hits:
            summary_writer.writerow(flatten_hit_for_summary(h))
            total_docs += 1

            json_path, json_bytes = save_json_for_hit(
                h, json_dir, 
                skip_existing=skip_existing,
                pretty_print=pretty_print,
                include_metadata=include_metadata,
                replace_text=replace_text
            )
            if json_path or json_bytes > 0:
                json_writer.writerow(flatten_hit_for_json_inventory(h, json_path, json_bytes))
                total_json_rows += 1

        print(f"Exported {len(hits)} docs (total {total_docs}). JSON files so far: {total_json_rows}")

        # Scan remaining pages
        while True:
            if not scroll_id:
                break
            try:
                page = client.scroll(scroll_id=scroll_id, scroll=scroll_keepalive)
            except TransportError as e:
                print(f"Scroll request failed: {e}", file=sys.stderr)
                break

            scroll_id = page.get("_scroll_id")
            hits = page.get("hits", {}).get("hits", [])
            if not hits:
                break

            for h in hits:
                summary_writer.writerow(flatten_hit_for_summary(h))
                total_docs += 1

                json_path, json_bytes = save_json_for_hit(
                    h, json_dir,
                    skip_existing=skip_existing,
                    pretty_print=pretty_print,
                    include_metadata=include_metadata,
                    replace_text=replace_text
                )
                if json_path or json_bytes > 0:
                    json_writer.writerow(flatten_hit_for_json_inventory(h, json_path, json_bytes))
                    total_json_rows += 1

            print(f"Exported {len(hits)} docs (total {total_docs}). JSON files so far: {total_json_rows}")

    # Best-effort clear
    if scroll_id:
        try:
            client.clear_scroll(scroll_id=scroll_id)
        except Exception:
            pass

    return (total_docs, total_json_rows)

# ---------- Main ----------

def main() -> int:
    args = parse_args()
    load_env()

    start = time.time()
    print("Connecting to OpenSearch...")
    client = connect_opensearch()
    if client is None:
        print("Failed to create OpenSearch client. Exiting.", file=sys.stderr)
        return 2

    # Validate dates
    try:
        _ = datetime.date.fromisoformat(args.start_date)
        _ = datetime.date.fromisoformat(args.end_date)
    except ValueError:
        print("Dates must be in YYYY-MM-DD format.", file=sys.stderr)
        return 3

    # Build query
    body = build_query(args.start_date, args.end_date, args.hostname, args.url_path)
    print(f"Querying indexes: {args.index_pattern}")
    print(f"Date range (inclusive): {args.start_date} .. {args.end_date}")
    if args.hostname:
        print(f"Filtering by hostname: {args.hostname}")
    else:
        print("No hostname filter applied.")
    if args.url_path:
        print(f"Filtering by URL path pattern: {args.url_path}")
    else:
        print("No URL path filter applied.")
    print(f"Scroll size: {args.scroll_size}, keepalive: {args.timeout}")
    print(f"JSON format: {'pretty-printed' if args.pretty_print else 'compact'}")
    print(f"Include metadata: {args.include_metadata}")
    print(f"Text replacement: {'Enabled (Heartland -> Auris)' if args.replace_text else 'Disabled'}")

    # Filenames
    date_prefix = datetime.datetime.now().strftime("%Y-%m-%d")
    ensure_output_dir(args.output_dir)

    summary_csv_name = f"{date_prefix}_webspider_marketing.csv"  # original CSV
    summary_csv_path = os.path.join(args.output_dir, summary_csv_name)

    if args.json_csv_name:
        json_csv_name = args.json_csv_name
    else:
        json_csv_name = f"json_exports_{date_prefix}.csv"
    json_csv_path = os.path.join(args.output_dir, json_csv_name)

    # Export
    total_docs, total_json_rows = export_with_scroll(
        client=client,
        index_pattern=args.index_pattern,
        body=body,
        summary_csv_path=summary_csv_path,
        json_csv_path=json_csv_path,
        json_dir=args.json_dir,
        scroll_size=args.scroll_size,
        scroll_keepalive=args.timeout,
        skip_existing=args.skip_existing,
        pretty_print=args.pretty_print,
        include_metadata=args.include_metadata,
        replace_text=args.replace_text,
    )

    elapsed = time.time() - start
    print(f"Done. Streamed {total_docs} documents in {elapsed:.2f}s.")
    print(f"Summary CSV: {summary_csv_path}")
    print(f"JSON inventory CSV: {json_csv_path}")
    print(f"JSON directory: {args.json_dir}")
    print(f"JSON files written (created or already present): {total_json_rows}")
    return 0 if total_docs >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())