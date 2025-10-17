#!/usr/bin/env python3
"""
Export OpenSearch docs to:
  1) A summary CSV (original behavior unchanged)
  2) Per-doc JSON files using _id as <_id>.json (filtered fields only)
  3) A second CSV listing every created JSON file

Enhancements:
  - Only export specific allowlisted fields.
  - Exclude html_source_code entirely.
  - Add html_parsed_content = inner <main> HTML (without <main> wrapper).
  - Remove all <script> tags from <main>.
  - Compute html_length / html_size from cleaned content.
  - Replace 'www.heartland.us' → 'www.auris.io' (and Heartland → Auris) when --replace-text is used.
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
from bs4 import BeautifulSoup
from opensearchpy import OpenSearch, RequestsHttpConnection
from opensearchpy.exceptions import TransportError, ConnectionError  # type: ignore
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------- Configuration ----------
DEFAULT_INDEX_PATTERN = "pyspider-mk-gpn-2025-*"
SCROLL_KEEPALIVE = "2m"
SCROLL_SIZE = 1000

CSV_COLUMNS = ["source", "url", "final_url", "hostname", "domain", "http_status", "url_mismatch", "url_path"]
JSON_INV_COLUMNS = ["id", "index", "json_file", "json_bytes", "url", "final_url", "hostname", "domain", "http_status", "url_path"]

ALLOWED_FIELDS = [
    "url", "final_url", "hostname", "url_path", "inventory_id",
    "html_length", "html_size", "first_http_status", "first_http_text",
    "final_http_status", "final_http_text", "actual_redirects", "http_status",
    "page_last_modified", "redirect_destination", "redirect_count",
    "canonical_url", "title", "meta_keywords", "meta_description",
    "robots_meta", "og_title", "og_description", "og_image", "og_url",
    "twitter_title", "twitter_description", "twitter_image", "html_source_code"
]

REPLACEMENT_SKIP_KEYS = {"html_source_code"}

# ---------- Helpers ----------

def human_size(num_bytes: int) -> str:
    step = 1024.0
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for u in units:
        if size < step or u == units[-1]:
            if u == "B":
                return f"{int(size)} {u}"
            return f"{size:.2f} {u}"
        size /= step
    return f"{size:.2f} TB"

def build_query(start_date: str, end_date: str, hostname: Optional[str] = None, url_path_pattern: Optional[str] = None) -> Dict[str, Any]:
    gte = f"{start_date}T00:00:00Z"
    lte = f"{end_date}T23:59:59Z"
    must = [{"range": {"creation_date": {"gte": gte, "lte": lte}}}]
    if hostname:
        must.append({"term": {"hostname": hostname}})
    if url_path_pattern:
        must.append({"wildcard": {"url_path": url_path_pattern}})
    return {"query": {"bool": {"must": must}}}

def ensure_output_dir(path: str) -> None:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)

# ---------- Replacement logic ----------

def _replace_text_in_value(value: Any) -> Any:
    if isinstance(value, str):
        result = re.sub(r"\bwww\.heartland\.us\b", "www.auris.io", value, flags=re.IGNORECASE)
        result = re.sub(r"Heartland's", "Auris'", result, flags=re.IGNORECASE)
        result = re.sub(r"Heartland", "Auris", result, flags=re.IGNORECASE)
        return result
    return value

def replace_heartland_text(obj: Any) -> Any:
    if isinstance(obj, dict):
        new_obj = {}
        for k, v in obj.items():
            if k in REPLACEMENT_SKIP_KEYS:
                new_obj[k] = v
            else:
                new_obj[k] = replace_heartland_text(v)
        return new_obj
    elif isinstance(obj, list):
        return [replace_heartland_text(i) for i in obj]
    else:
        return _replace_text_in_value(obj)

# ---------- HTML parsing logic ----------

def project_source_allowlisted(source: Dict[str, Any]) -> Dict[str, Any]:
    """
    Keep only allowed fields (excluding html_source_code),
    extract <main> inner HTML (without <main> wrapper),
    remove <script> tags, and store in html_parsed_content.
    """
    projected: Dict[str, Any] = {}

    # Copy allowed fields except html_source_code
    for key in ALLOWED_FIELDS:
        if key == "html_source_code":
            continue
        if key in source:
            projected[key] = source[key]

    html = source.get("html_source_code")
    parsed_main_html = ""

    if isinstance(html, str):
        try:
            soup = BeautifulSoup(html, "html.parser")
            mains = soup.find_all("main")
            if mains:
                fragments = []
                for tag in mains:
                    for s in tag.find_all("script"):
                        s.decompose()
                    inner_html = "".join(str(child) for child in tag.contents)
                    fragments.append(inner_html)
                parsed_main_html = "".join(fragments)
            else:
                parsed_main_html = ""
        except Exception as e:
            print(f"Error parsing HTML for <main>: {e}", file=sys.stderr)
            parsed_main_html = ""

    projected["html_parsed_content"] = parsed_main_html

    if parsed_main_html:
        blen = len(parsed_main_html.encode("utf-8", errors="ignore"))
        projected["html_length"] = blen
        projected["html_size"] = human_size(blen)
    else:
        projected["html_length"] = 0
        projected["html_size"] = "0 B"

    return projected

# ---------- Connection ----------

def connect_opensearch() -> Optional[OpenSearch]:
    load_dotenv(override=False)
    OS_USERNAME = os.getenv("OPENSEARCH_USERNAME", "admin")
    OS_PASSWORD = os.getenv("OPENSEARCH_PASSWORD", "admin")
    OS_HOSTNAME = os.getenv("OPENSEARCH_HOSTNAME", "opensearch-node")

    try:
        client = OpenSearch(
            hosts=[f"https://{OS_HOSTNAME}:9200"],
            http_auth=(OS_USERNAME, OS_PASSWORD),
            use_ssl=True,
            verify_certs=False,
            connection_class=RequestsHttpConnection,
            ssl_show_warn=False,
        )
        if not client.ping():
            print("Connected but ping failed.", file=sys.stderr)
            return None
        return client
    except ConnectionError as e:
        print("Connection error:", e, file=sys.stderr)
        return None

# ---------- Export logic ----------

def flatten_hit_for_summary(hit: Dict[str, Any]) -> Dict[str, Any]:
    src = hit.get("_source", {}) or {}
    url = src.get("url", "")
    final_url = src.get("final_url", "")
    return {
        "source": hit.get("_index", ""),
        "url": url,
        "final_url": final_url,
        "hostname": src.get("hostname", ""),
        "domain": src.get("domain", ""),
        "http_status": src.get("http_status", ""),
        "url_mismatch": "Yes" if url != final_url else "No",
        "url_path": src.get("url_path", ""),
    }

def save_json_for_hit(hit: Dict[str, Any], json_dir: str, skip_existing: bool,
                      pretty_print: bool, replace_text: bool) -> Tuple[Optional[str], int]:
    _id = hit.get("_id")
    if not _id:
        return (None, 0)
    src = hit.get("_source", {}) or {}
    if not src:
        return (None, 0)

    export_data = project_source_allowlisted(src)
    if replace_text:
        export_data = replace_heartland_text(export_data)

    ensure_output_dir(json_dir)
    out_path = os.path.join(json_dir, f"{_id}.json")

    if skip_existing and os.path.exists(out_path):
        return (out_path, os.path.getsize(out_path))

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2 if pretty_print else None)
    return (out_path, os.path.getsize(out_path))

def flatten_hit_for_json_inventory(hit: Dict[str, Any], json_file: str, json_bytes: int) -> Dict[str, Any]:
    src = hit.get("_source", {}) or {}
    return {
        "id": hit.get("_id", ""),
        "index": hit.get("_index", ""),
        "json_file": json_file or "",
        "json_bytes": json_bytes,
        "url": src.get("url", ""),
        "final_url": src.get("final_url", ""),
        "hostname": src.get("hostname", ""),
        "domain": src.get("domain", ""),
        "http_status": src.get("http_status", ""),
        "url_path": src.get("url_path", ""),
    }

# ---------- Main Scroll Export ----------

def export_with_scroll(client: OpenSearch, index_pattern: str, body: Dict[str, Any],
                       summary_csv_path: str, json_csv_path: str, json_dir: str,
                       scroll_size: int, scroll_keepalive: str,
                       skip_existing: bool, pretty_print: bool, replace_text: bool) -> Tuple[int, int]:
    total_docs = total_json = 0
    try:
        page = client.search(index=index_pattern, body=body, size=scroll_size, scroll=scroll_keepalive)
    except TransportError as e:
        print(f"Initial search failed: {e}", file=sys.stderr)
        return (0, 0)

    scroll_id = page.get("_scroll_id")
    hits = page.get("hits", {}).get("hits", [])

    ensure_output_dir(os.path.dirname(summary_csv_path) or ".")
    ensure_output_dir(os.path.dirname(json_csv_path) or ".")
    ensure_output_dir(json_dir)

    with open(summary_csv_path, "w", newline="", encoding="utf-8") as sf, \
         open(json_csv_path, "w", newline="", encoding="utf-8") as jf:
        summary_writer = csv.DictWriter(sf, fieldnames=CSV_COLUMNS)
        summary_writer.writeheader()
        json_writer = csv.DictWriter(jf, fieldnames=JSON_INV_COLUMNS)
        json_writer.writeheader()

        while hits:
            for h in hits:
                summary_writer.writerow(flatten_hit_for_summary(h))
                total_docs += 1
                json_path, json_bytes = save_json_for_hit(h, json_dir, skip_existing, pretty_print, replace_text)
                if json_path:
                    json_writer.writerow(flatten_hit_for_json_inventory(h, json_path, json_bytes))
                    total_json += 1
            try:
                page = client.scroll(scroll_id=scroll_id, scroll=scroll_keepalive)
                hits = page.get("hits", {}).get("hits", [])
                scroll_id = page.get("_scroll_id")
            except TransportError as e:
                print(f"Scroll failed: {e}", file=sys.stderr)
                break

    if scroll_id:
        try:
            client.clear_scroll(scroll_id=scroll_id)
        except Exception:
            pass
    return (total_docs, total_json)

# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Export OpenSearch docs to CSV + JSON (filtered).")
    parser.add_argument("--index-pattern", default=DEFAULT_INDEX_PATTERN)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--hostname")
    parser.add_argument("--url-path")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--json-dir", default="output/json")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--pretty-print", action="store_true")
    parser.add_argument("--replace-text", action="store_true")
    args = parser.parse_args()

    client = connect_opensearch()
    if not client:
        print("OpenSearch connection failed.", file=sys.stderr)
        return 1

    body = build_query(args.start_date, args.end_date, args.hostname, args.url_path)
    ensure_output_dir(args.output_dir)
    date_prefix = datetime.datetime.now().strftime("%Y-%m-%d")
    summary_csv = os.path.join(args.output_dir, f"{date_prefix}_summary.csv")
    json_csv = os.path.join(args.output_dir, f"{date_prefix}_json_exports.csv")

    total_docs, total_json = export_with_scroll(
        client, args.index_pattern, body,
        summary_csv, json_csv, args.json_dir,
        SCROLL_SIZE, SCROLL_KEEPALIVE,
        args.skip_existing, args.pretty_print, args.replace_text
    )

    print(f"Done. Streamed {total_docs} docs, wrote {total_json} JSON files.")
    print(f"Summary CSV: {summary_csv}")
    print(f"JSON inventory: {json_csv}")
    print(f"JSON directory: {args.json_dir}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
