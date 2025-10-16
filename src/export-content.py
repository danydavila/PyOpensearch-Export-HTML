#!/usr/bin/env python3
"""
Export OpenSearch docs to:
  1) A summary CSV (original behavior unchanged)
  2) Per-doc HTML files using _id as <_id>.html
  3) A second CSV listing every created HTML file

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
import os
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
CSV_COLUMNS = ["source", "url", "final_url", "hostname", "domain", "http_status", "url_mismatch"]

# New HTML export inventory CSV columns
HTML_INV_COLUMNS = [
    "id", "index", "html_file", "html_bytes",
    "url", "final_url", "hostname", "domain", "http_status"
]

# ---------- Helpers ----------

def build_query(start_date: str, end_date: str, hostname: Optional[str] = None) -> Dict[str, Any]:
    """
    Build a bool query combining:
      - range on 'creation_date' (inclusive day bounds)
      - optional exact-match term on 'hostname' if provided
    """
    gte = f"{start_date}T00:00:00Z"
    lte = f"{end_date}T23:59:59Z"

    must_clauses: List[Dict[str, Any]] = [
        {"range": {"creation_date": {"gte": gte, "lte": lte}}}
    ]

    if hostname:
        must_clauses.append({"term": {"hostname": hostname}})

    return {"query": {"bool": {"must": must_clauses}}}

def ensure_output_dir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export OpenSearch docs to CSV + per-doc HTML via Scroll API.")
    parser.add_argument("--index-pattern", default=DEFAULT_INDEX_PATTERN, help="Index pattern to search.")
    parser.add_argument("--start-date", required=True, help="Start date (YYYY-MM-DD).")
    parser.add_argument("--end-date", required=True, help="End date (YYYY-MM-DD).")
    parser.add_argument("--hostname", help="Optional: exact hostname filter (e.g., www.contoso.com).")
    parser.add_argument("--output-dir", default="output", help="Directory to write CSV files.")
    parser.add_argument("--scroll-size", type=int, default=SCROLL_SIZE, help="Scroll page size (default 1000).")
    parser.add_argument("--timeout", default=SCROLL_KEEPALIVE, help="Scroll keepalive (e.g., 2m).")

    # New: HTML export path + inventory CSV name
    parser.add_argument("--html-dir", default="output/html", help="Directory where <_id>.html files will be written.")
    parser.add_argument("--html-csv-name", default=None,
                        help="Optional override for HTML inventory CSV filename. Default: html_exports_<YYYY-MM-DD>.csv")
    parser.add_argument("--skip-existing", action="store_true",
                        help="If set, do not overwrite existing HTML files.")

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
    }

def save_html_for_hit(
    hit: Dict[str, Any],
    html_dir: str,
    skip_existing: bool = False,
) -> Tuple[Optional[str], int]:
    """
    Save the hit's html_source_code to <html_dir>/<id>.html.
    Returns (filepath or None if not saved, bytes_written).
    """
    _id = hit.get("_id")
    if not _id:
        return (None, 0)

    source = hit.get("_source", {}) or {}
    html = source.get("html_source_code", None)
    if not isinstance(html, str) or html == "":
        return (None, 0)

    ensure_output_dir(html_dir)
    out_path = os.path.join(html_dir, f"{_id}.html")

    if skip_existing and os.path.exists(out_path):
        try:
            return (out_path, os.path.getsize(out_path))
        except OSError:
            # If stat fails, proceed to write
            pass

    try:
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            f.write(html)
        size = os.path.getsize(out_path)
        return (out_path, size)
    except Exception as e:
        print(f"Failed to write HTML for id={_id}: {e}", file=sys.stderr)
        return (None, 0)

def flatten_hit_for_html_inventory(
    hit: Dict[str, Any],
    html_file: Optional[str],
    html_bytes: int,
) -> Dict[str, Any]:
    """Flat row for the HTML export inventory CSV."""
    source = hit.get("_source", {}) or {}
    return {
        "id": hit.get("_id", ""),
        "index": hit.get("_index", ""),
        "html_file": html_file or "",
        "html_bytes": html_bytes,
        "url": source.get("url", ""),
        "final_url": source.get("final_url", ""),
        "hostname": source.get("hostname", ""),
        "domain": source.get("domain", ""),
        "http_status": source.get("http_status", ""),
    }

# ---------- Export logic ----------

def export_with_scroll(
    client: OpenSearch,
    index_pattern: str,
    body: Dict[str, Any],
    summary_csv_path: str,
    html_csv_path: str,
    html_dir: str,
    scroll_size: int,
    scroll_keepalive: str,
    skip_existing: bool,
) -> Tuple[int, int]:
    """
    Execute a scroll search, stream results to:
      - summary CSV (original behavior)
      - per-file HTML inventory CSV
      - write HTML files to html_dir
    Returns (total_docs_streamed, total_html_files_written_or_skipped).
    """
    total_docs = 0
    total_html_rows = 0

    print(f"[{datetime.datetime.utcnow().isoformat()}Z] Starting initial search...")
    try:
        page = client.search(
            index=index_pattern,
            body=body,
            size=scroll_size,
            scroll=scroll_keepalive,
            _source_includes=[
                "url", "final_url", "hostname", "domain", "http_status",
                "html_source_code"  # NEW
            ],
        )
    except TransportError as e:
        print(f"Initial search failed: {e}", file=sys.stderr)
        return (0, 0)

    scroll_id = page.get("_scroll_id")
    hits = page.get("hits", {}).get("hits", [])

    # Ensure dirs
    ensure_output_dir(os.path.dirname(summary_csv_path) or ".")
    ensure_output_dir(os.path.dirname(html_csv_path) or ".")
    ensure_output_dir(html_dir)

    with open(summary_csv_path, "w", newline="", encoding="utf-8") as summary_f, \
         open(html_csv_path, "w", newline="", encoding="utf-8") as html_f:

        summary_writer = csv.DictWriter(summary_f, fieldnames=CSV_COLUMNS)
        summary_writer.writeheader()

        html_writer = csv.DictWriter(html_f, fieldnames=HTML_INV_COLUMNS)
        html_writer.writeheader()

        # write first page
        for h in hits:
            summary_writer.writerow(flatten_hit_for_summary(h))
            total_docs += 1

            html_path, html_bytes = save_html_for_hit(h, html_dir, skip_existing=skip_existing)
            if html_path or html_bytes > 0:
                html_writer.writerow(flatten_hit_for_html_inventory(h, html_path, html_bytes))
                total_html_rows += 1

        print(f"Exported {len(hits)} docs (total {total_docs}). HTML rows so far: {total_html_rows}")

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

                html_path, html_bytes = save_html_for_hit(h, html_dir, skip_existing=skip_existing)
                if html_path or html_bytes > 0:
                    html_writer.writerow(flatten_hit_for_html_inventory(h, html_path, html_bytes))
                    total_html_rows += 1

            print(f"Exported {len(hits)} docs (total {total_docs}). HTML rows so far: {total_html_rows}")

    # Best-effort clear
    if scroll_id:
        try:
            client.clear_scroll(scroll_id=scroll_id)
        except Exception:
            pass

    return (total_docs, total_html_rows)

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
    body = build_query(args.start_date, args.end_date, args.hostname)
    print(f"Querying indexes: {args.index_pattern}")
    print(f"Date range (inclusive): {args.start_date} .. {args.end_date}")
    if args.hostname:
        print(f"Filtering by hostname: {args.hostname}")
    else:
        print("No hostname filter applied.")
    print(f"Scroll size: {args.scroll_size}, keepalive: {args.timeout}")

    # Filenames
    date_prefix = datetime.datetime.now().strftime("%Y-%m-%d")
    ensure_output_dir(args.output_dir)

    summary_csv_name = f"{date_prefix}_webspider_marketing.csv"  # original CSV
    summary_csv_path = os.path.join(args.output_dir, summary_csv_name)

    if args.html_csv_name:
        html_csv_name = args.html_csv_name
    else:
        html_csv_name = f"html_exports_{date_prefix}.csv"
    html_csv_path = os.path.join(args.output_dir, html_csv_name)

    # Export
    total_docs, total_html_rows = export_with_scroll(
        client=client,
        index_pattern=args.index_pattern,
        body=body,
        summary_csv_path=summary_csv_path,
        html_csv_path=html_csv_path,
        html_dir=args.html_dir,
        scroll_size=args.scroll_size,
        scroll_keepalive=args.timeout,
        skip_existing=args.skip_existing,
    )

    elapsed = time.time() - start
    print(f"Done. Streamed {total_docs} documents in {elapsed:.2f}s.")
    print(f"Summary CSV: {summary_csv_path}")
    print(f"HTML inventory CSV: {html_csv_path}")
    print(f"HTML directory: {args.html_dir}")
    print(f"HTML rows written (files created or already present): {total_html_rows}")
    return 0 if total_docs >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
